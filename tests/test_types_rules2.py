"""BITS, INTERVAL, TIMETZ, LIST, INET and VARIANT: the rules, and planted
differences found exactly.

These were the fourteen native types still mapped to UNKNOWN after TIME,
BINARY and JSON got their rules (tests/test_types_rules.py): compared through
each engine's text cast, outside the sample check. That text was not always
injective - DuckDB prints the TIMETZ offset +05:00:30 as `+05:30`, the
offset +05:30's own text (COV-7, a false match), and SQL Server's style 3
for a sql_variant float raised on -0.0 (COV-8) - and it rarely matched
another engine's (`{a,b}` against `[a, b]`). Each type now has an executable
spec in `dialects/base.py`, the reference reads it, and `tests/conformance`
holds every engine's SQL to it value by value.

This file pins the specs offline and, live, plants the changes a lossy
rendering would hide - one day against 24 hours, an offset to the second, a
bit string's width, a list's shape (`[]`, `['']`, `[NULL]`, `['a,b']`,
`['a', 'b']`), a /32 against a /8, int 1 against bigint 1 - and requires
the walk to find exactly those rows and columns, with the sample check
covering every column (CLAUDE.md section 8).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from decimal import Decimal

import pytest
from conftest import duckdb_write, mysql_rw_cursor, open_duckdb, open_mysql, open_pg

from parity.dialects.base import (
    LIST_TERMINATOR,
    NULL_SENTINEL,
    Dialect,
    canonical_bits_text,
    canonical_inet_text,
    canonical_interval_text,
    canonical_list_text,
    canonical_timetz_text,
    canonical_variant_text,
    escape_canonical_text,
    map_type,
)
from parity.dialects.duckdb_dialect import DuckDBDialect
from parity.dialects.mssql_dialect import _variant_value, map_type_mssql
from parity.dialects.mysql_dialect import MySQLDialect, map_type_mysql
from parity.dialects.postgres_dialect import PostgresDialect
from parity.engine import diff
from parity.reference import UNCHECKED, canonical_text, column_token, reference_token
from parity.types import Column, KeySpec, LogicalType

# ------------------------------------------------------------- the specs


def test_bits_text_is_every_digit():
    """The width is part of the value; only 0 and 1 are digits."""
    assert canonical_bits_text("00000101") == "00000101"
    assert canonical_bits_text("101") != canonical_bits_text("0101")
    assert canonical_bits_text("") == ""
    assert canonical_bits_text("") != NULL_SENTINEL
    with pytest.raises(ValueError):
        canonical_bits_text("012")


def test_interval_text_keeps_the_three_components():
    """`1 day`, `24:00:00`, `1 mon` and `30 days` are four values."""
    texts = {
        canonical_interval_text(0, 1, 0),
        canonical_interval_text(0, 0, 86_400_000_000),
        canonical_interval_text(1, 0, 0),
        canonical_interval_text(0, 30, 0),
    }
    assert len(texts) == 4
    assert canonical_interval_text(-14, 3, -1) == "-14 mon 3 day -1 us"


@pytest.mark.parametrize(
    ("text", "parts"),
    [
        # PostgreSQL, IntervalStyle postgres (pinned at connect).
        ("-1 years -2 mons +3 days -04:05:06.000007", (-14, 3, -14_706_000_007)),
        ("1 day", (0, 1, 0)),
        ("24:00:00", (0, 0, 86_400_000_000)),
        ("-2562047788:00:54.775808", (0, 0, -9_223_372_036_854_775_808)),
        ("1 mon -00:00:00.000001", (1, 0, -1)),
        ("00:00:00", (0, 0, 0)),
        # DuckDB.
        ("1 year 2 months 3 days 04:05:06.000007", (14, 3, 14_706_000_007)),
        ("-1 year 2 days -00:00:00.000001", (-12, 2, -1)),
        ("1 month 2 days 00:00:00.000003", (1, 2, 3)),
    ],
)
def test_reference_reads_both_engines_interval_text(text: str, parts):
    """Text in, the stored components out - whichever engine printed it."""
    token = reference_token(LogicalType.INTERVAL, text, 6)
    assert canonical_text(token) == canonical_interval_text(*parts)


def test_reference_refuses_a_timedelta_interval():
    """psycopg and DuckDB's driver fold a month into 30 days: never guess."""
    token = reference_token(LogicalType.INTERVAL, dt.timedelta(days=30), 6)
    assert token is UNCHECKED
    assert reference_token(LogicalType.INTERVAL, "infinity", 6) == (
        "interval",
        "infinity",
    )


def test_timetz_text_keeps_offset_seconds():
    """+05:00:30 and +05:30 are different offsets (COV-7)."""
    noon = 12 * 3600 * 1_000_000
    assert canonical_timetz_text(noon, 18030) == "12:00:00.000000+05:00:30"
    assert canonical_timetz_text(noon, 19800) == "12:00:00.000000+05:30"
    assert canonical_timetz_text(0, -57599) == "00:00:00.000000-15:59:59"
    assert canonical_timetz_text(86_400_000_000, 0) == "24:00:00.000000+00:00"
    tz = dt.timezone(dt.timedelta(seconds=18030))
    token = reference_token(LogicalType.TIMETZ, dt.time(12, 0, tzinfo=tz), 6)
    assert canonical_text(token) == "12:00:00.000000+05:00:30"
    text = reference_token(LogicalType.TIMETZ, "24:00:00-15:59:59", 6)
    assert canonical_text(text) == "24:00:00.000000-15:59:59"
    with pytest.raises(ValueError):
        canonical_text(reference_token(LogicalType.TIMETZ, dt.time(12, 0), 6))


@pytest.mark.parametrize(
    ("value", "text"),
    [
        # Each verified against PostgreSQL 18.4's `inet::text`.
        ("10.0.0.1", "10.0.0.1/32"),
        ("10.0.0.1/8", "10.0.0.1/8"),
        ("::1", "::1/128"),
        ("::", "::/128"),
        ("::/0", "::/0"),
        ("::ffff:1.2.3.4", "::ffff:1.2.3.4/128"),
        ("::1.2.3.4", "::1.2.3.4/128"),
        ("::ffff:0:0", "::ffff:0.0.0.0/128"),
        ("1:0:0:1:0:0:0:1", "1:0:0:1::1/128"),
        ("1:0:0:1:0:0:1:1", "1::1:0:0:1:1/128"),
        ("1:0:1:1:1:1:1:1", "1:0:1:1:1:1:1:1/128"),
        ("FE80::1", "fe80::1/128"),
        ("1::", "1::/128"),
        ("::ffff:1.2.3.4/96", "::ffff:1.2.3.4/96"),
    ],
)
def test_inet_text_is_postgresql_text(value: str, text: str):
    """BIND's inet_net_ntop rules, and every prefix written out."""
    iface = ipaddress.ip_interface(value)
    assert canonical_inet_text(iface.ip.packed, iface.network.prefixlen) == text
    assert canonical_text(reference_token(LogicalType.INET, value, 6)) == text


def test_reference_reads_psycopg_inet_objects():
    """inet loads as an address or an interface, cidr as a network."""
    t = LogicalType.INET
    host = reference_token(t, ipaddress.ip_address("10.0.0.1"), 6)
    assert canonical_text(host) == "10.0.0.1/32"
    iface = reference_token(t, ipaddress.ip_interface("10.0.0.1/8"), 6)
    assert canonical_text(iface) == "10.0.0.1/8"
    net = reference_token(t, ipaddress.ip_network("10.0.0.0/8"), 6)
    assert canonical_text(net) == "10.0.0.0/8"


LISTS = [
    [],
    [""],
    [None],
    ["", ""],
    [None, None],
    ["", None],
    ["a,b"],
    ["a", "b"],
    ["a, b"],
    ["NULL"],
    ["\\N"],
    ["\\"],
    ["a\\,b"],
    ["a\\", ",b"],
    ["a\\", "\\,b"],
    ["x\\,y"],
    ["x", "y"],
    ["a\x1fb"],
    ["a", "\x1fb"],
    ["]"],
    ["[", "]"],
]


def _list_text(items: list) -> str:
    """A list of strings by the rule: each element as escaped text."""
    return canonical_list_text(
        [NULL_SENTINEL if x is None else escape_canonical_text(x) for x in items]
    )


def test_list_text_is_injective():
    """Every list above renders to its own text, none holds chr(31), and
    none is the NULL sentinel."""
    texts = [_list_text(x) for x in LISTS]
    assert len(set(texts)) == len(LISTS), texts
    assert all("\x1f" not in t and t != NULL_SENTINEL for t in texts)
    assert _list_text([]) == "[]"
    assert _list_text([""]) == "[" + LIST_TERMINATOR + "]"
    assert _list_text([None]) == "[" + NULL_SENTINEL + LIST_TERMINATOR + "]"


def test_list_reference_reduces_each_element_by_its_rule():
    """DECIMAL elements round as a DECIMAL column does; an element the rules
    cannot read leaves the whole list unchecked."""
    d = PostgresDialect()
    col = Column("n", LogicalType.LIST, "numeric(10,2)[]")
    token = column_token(d, col, [Decimal("0.0000005"), None])
    assert canonical_text(token) == "[0.000001\\,\\N\\,]"
    text = Column("t", LogicalType.LIST, "text[]")
    assert column_token(d, text, [b"bytes"]) is UNCHECKED
    assert column_token(d, text, None) is None


VARIANTS = [
    (("int", 1), "int:1"),
    (("bigint", 1), "bigint:1"),
    (("nvarchar", "1"), "nvarchar:1"),
    (("varchar", "1"), "varchar:1"),
    (("decimal", Decimal("1.50")), "decimal:1.50"),
    (("decimal", Decimal("1.5")), "decimal:1.5"),
    (("float", 1.5), "float:0x3FF8000000000000"),
    (("float", -0.0), "float:0x0000000000000000"),
    (("float", -1e-300), "float:0x81A56E1FC2F8F359"),
    (("real", 1.5), "real:0x3FC00000"),
    (("money", Decimal("-1.5")), "money:-1.5000"),
    (
        ("datetime", (dt.date(2024, 1, 2), 11_045_007_000_000)),
        "datetime:2024-01-02 03:04:05.007",
    ),
    (
        ("smalldatetime", (dt.date(2024, 1, 2), 11_040_000_000_000)),
        "smalldatetime:2024-01-02 03:04:00",
    ),
    (
        ("datetime2", (dt.date(1, 1, 2), 11_045_123_000_000)),
        "datetime2:0001-01-02 03:04:05.1230000",
    ),
    (
        ("datetimeoffset", (dt.date(2024, 1, 2), 11_045_123_456_700, -345)),
        "datetimeoffset:2024-01-02 03:04:05.1234567 -05:45",
    ),
    (("time", 86_399_999_999_900), "time:23:59:59.9999999"),
    (("date", dt.date(1, 1, 2)), "date:0001-01-02"),
    (("binary", b"\x00\xab\x00"), "binary:0x00AB00"),
    (
        ("uniqueidentifier", "6F9619FF-8B86-D011-B42D-00C04FC964FF"),
        "uniqueidentifier:6f9619ff-8b86-d011-b42d-00c04fc964ff",
    ),
    (("nvarchar", "\\N\x1f"), "nvarchar:\\\\N\\s"),
    (("char", "ab  "), "char:ab  "),
]


@pytest.mark.parametrize(("value", "text"), VARIANTS)
def test_variant_text(value, text: str):
    """Each base type's exact spelling - every one verified live against
    SQL Server 2022's rendering."""
    assert canonical_variant_text(*value) == text


def test_variant_text_is_injective_across_base_types():
    """int 1, bigint 1, N'1' and '1' never meet."""
    texts = [t for _, t in VARIANTS]
    assert len(set(texts)) == len(texts)
    with pytest.raises(ValueError):
        canonical_variant_text("xml", "<a/>")


@pytest.mark.parametrize(
    ("payload", "value"),
    [
        # Payloads exactly as SQL Server 2022 returned them (`sample_select`).
        ("tinyint::C8", ("tinyint", 200)),
        ("smallint::FFFE", ("smallint", -2)),
        ("bigint::FFFFFFFFFFFFFFFE", ("bigint", -2)),
        ("decimal::0502000096000000", ("decimal", Decimal("-1.50"))),
        ("numeric::0A030001DC050000", ("numeric", Decimal("1.500"))),
        (
            "decimal::26000000FFFFFFFF3F228A097AC4865AA84C3B4B",
            ("decimal", Decimal("-99999999999999999999999999999999999999")),
        ),
        ("float::BFB999999999999A", ("float", -0.1)),
        ("money::FFFFFFFFFFFFC568", ("money", Decimal("-1.5000"))),
        (
            "datetime::0000B0EB00328F5E",
            ("datetime", (dt.date(2024, 1, 2), 11_045_007_000_000)),
        ),
        (
            "smalldatetime::B0EB00B8",
            ("smalldatetime", (dt.date(2024, 1, 2), 11_040_000_000_000)),
        ),
        ("date::010000", ("date", dt.date(1, 1, 2))),
        ("time::07FFBF692AC9", ("time", 86_399_999_999_900)),
        ("time::008B0E00", ("time", 3_723_000_000_000)),
        (
            "datetime2::030389A800010000",
            ("datetime2", (dt.date(1, 1, 2), 11_045_123_000_000)),
        ),
        (
            "datetimeoffset::07076D91E94946460BA7FE",
            ("datetimeoffset", (dt.date(2024, 1, 2), 11_045_123_456_700, -345)),
        ),
        ("nvarchar::E90020007800", ("nvarchar", "\u00e9 x")),
        ("varchar:65001:6162", ("varchar", "ab")),
        ("varchar:1252:E9", ("varchar", "\u00e9")),
    ],
)
def test_variant_payload_decodes_the_stored_bytes(payload: str, value):
    """The sample path's decoder, against the bytes SQL Server stores."""
    assert _variant_value(payload) == value


@pytest.mark.parametrize(
    ("mapper", "raw", "expected"),
    [
        (map_type, "bit", LogicalType.BITS),
        (map_type, "bit varying", LogicalType.BITS),
        (map_type, "BIT", LogicalType.BITS),
        (map_type, "interval", LogicalType.INTERVAL),
        (map_type, "INTERVAL", LogicalType.INTERVAL),
        (map_type, "time with time zone", LogicalType.TIMETZ),
        (map_type, "TIME WITH TIME ZONE", LogicalType.TIMETZ),
        (map_type, "time", LogicalType.TIME),
        (map_type, "inet", LogicalType.INET),
        (map_type, "cidr", LogicalType.INET),
        (map_type, "name", LogicalType.STRING),
        (map_type, "UHUGEINT", LogicalType.INTEGER),
        (map_type, "integer[]", LogicalType.LIST),
        (map_type, "character varying(10)[]", LogicalType.LIST),
        (map_type, "DECIMAL(10,2)[]", LogicalType.LIST),
        (map_type, "VARCHAR[]", LogicalType.LIST),
        (map_type, "INTEGER[][]", LogicalType.UNKNOWN),  # nested: no rule
        (map_type, "jsonb[]", LogicalType.UNKNOWN),
        (map_type, "STRUCT(a INTEGER)[]", LogicalType.UNKNOWN),
        (map_type, "public.mood[]", LogicalType.UNKNOWN),
        (map_type, "ARRAY", LogicalType.UNKNOWN),
        (map_type, "INTEGER[3]", LogicalType.UNKNOWN),  # fixed-size ARRAY
        (map_type_mysql, "bit", LogicalType.BITS),
        (map_type_mssql, "bit", LogicalType.BOOLEAN),  # never a bit string
        (map_type_mssql, "sql_variant", LogicalType.VARIANT),
    ],
)
def test_type_maps(mapper, raw: str, expected: LogicalType):
    """The new names, and the ones that must stay where they were."""
    assert mapper(raw) is expected


def test_a_list_key_is_refused():
    """Its elements round as values do, so distinct keys could merge."""
    d = DuckDBDialect(side="A")
    key = KeySpec((Column("k", LogicalType.LIST, "DECIMAL(10,2)[]"),), True)
    with pytest.raises(
        ValueError, match=r"side A.*list column 'k' cannot be used as a key"
    ):
        d.check_key("t", key)


def test_a_list_of_nanosecond_timestamps_is_refused():
    """Its digits past the microsecond would vanish without a caveat."""
    d = DuckDBDialect(side="B")
    col = Column("l", LogicalType.LIST, "TIMESTAMP_NS[]")
    with pytest.raises(ValueError, match="TIMESTAMP_NS elements"):
        d.normalize(col)


def test_uhugeint_keys_are_hashed():
    """No HUGEINT holds 2^128 - 1, so the key cannot be its own bucket."""
    d = DuckDBDialect()
    assert not d.bisects_natively(Column("k", LogicalType.INTEGER, "UHUGEINT"))
    assert d.bisects_natively(Column("k", LogicalType.INTEGER, "HUGEINT"))
    assert Dialect.bisects_natively(d, Column("k", LogicalType.INTEGER, "BIGINT"))


def test_mysql_bit_sample_restores_the_width():
    """The connector returns BIT(8) b'00000101' as the integer 5."""
    d = MySQLDialect()
    col = Column("b", LogicalType.BITS, "bit(8)")
    assert d.sample_value(col, 5) == "00000101"
    assert d.sample_value(Column("b", LogicalType.BITS, "bit"), 5) == "101"
    assert reference_token(LogicalType.BITS, 5, 6) is UNCHECKED


# ----------------------------------------------------- planted differences

N = 200

#: Keys whose value on side B differs from side A, and in which column.
PLANTED = {
    10: "iv",  # '1 day' -> '24 hours': equal under `=`, a different value
    20: "iv",  # '1 mon' -> '30 days'
    30: "tz",  # +05:30 -> +05:00:30: DuckDB's own text printed both as +05:30
    40: "tz",  # 12:00+05:45 -> 06:15+00: the same instant, another value
    50: "b",  # '0101' -> '101': the width is data
    60: "lt",  # ['a,b'] -> ['a', 'b']
    70: "lt",  # [] -> [NULL]
    80: "lt",  # [''] -> []
    90: "li",  # [1, 2] -> [2, 1]
    100: "li",  # NULL -> []
    110: "ld",  # one day later
    120: "lt",  # ['x\\,y'] -> ['x', 'y']: an element cannot forge a terminator
}


def _rows(side: str) -> list[tuple]:
    """(id, interval, timetz, bits, int list, text list, date list) - the
    same on both sides but for PLANTED. Intervals and offsets as text both
    engines parse."""
    rows = []
    for i in range(1, N + 1):
        iv: str | None = f"{i % 13} months {i % 29} days {i * 1_000_003} microseconds"
        tz: str | None = (
            f"{i % 24:02d}:{i % 60:02d}:{(i * 7) % 60:02d}+0{i % 10}:{(i * 15) % 60:02d}"
        )
        b: str | None = format(i, "b").zfill(i % 9 + 1)
        li: list | None = [i, None, -i] if i % 3 else []
        lt: list | None = [f"v{i}", "", None, "a,b"] if i % 4 else [f"{i}"]
        ld: list | None = [dt.date(2024, 1, 1) + dt.timedelta(days=i), None]
        if i % 17 == 0:
            iv = tz = b = li = lt = ld = None
        overrides = {
            10: {"iv": "1 day"},
            20: {"iv": "1 mon"},
            30: {"tz": "12:00:00+05:30"},
            40: {"tz": "12:00:00+05:45"},
            50: {"b": "0101"},
            60: {"lt": ["a,b"]},
            70: {"lt": []},
            80: {"lt": [""]},
            90: {"li": [1, 2]},
            100: {"li": None},
            120: {"lt": ["x\\,y"]},
        }
        if side == "B":
            overrides.update(
                {
                    10: {"iv": "24 hours"},
                    20: {"iv": "30 days"},
                    30: {"tz": "12:00:00+05:00:30"},
                    40: {"tz": "06:15:00+00"},
                    50: {"b": "101"},
                    60: {"lt": ["a", "b"]},
                    70: {"lt": [None]},
                    80: {"lt": []},
                    90: {"li": [2, 1]},
                    100: {"li": []},
                    120: {"lt": ["x", "y"]},
                }
            )
            if i == 110 and ld is not None:
                ld = [ld[0] + dt.timedelta(days=1), None]
        o = overrides.get(i, {})
        iv, tz, b = o.get("iv", iv), o.get("tz", tz), o.get("b", b)
        li, lt = o.get("li", li), o.get("lt", lt)
        rows.append((i, iv, tz, b, li, lt, ld))
    return rows


COLUMNS = {"iv", "tz", "b", "li", "lt", "ld"}


def _check(result, columns: set[str], planted: dict[int, str] = PLANTED) -> None:
    """Exactly the planted rows, in exactly their columns, all checked."""
    expected = {k: c for k, c in planted.items() if c in columns}
    found = {d.key: d.columns for d in result.diffs}
    assert {d.kind for d in result.diffs} <= {"different"}, result.diffs
    assert found == {k: [c] for k, c in expected.items()}, found
    v = result.verification
    assert v is not None and not v.failures and not v.error, v
    assert set(v.checked) >= columns and not (set(v.unchecked) & columns), v


@pytest.fixture(scope="module")
def duck(tmp_path_factory):
    """Both sides on DuckDB, planted; plus a UHUGEINT-keyed pair."""
    pytest.importorskip("duckdb")
    path = str(tmp_path_factory.mktemp("typ2") / "typ2.duckdb")
    con = duckdb_write(path)
    for side in ("A", "B"):
        con.execute(
            f"create table typ2_{side} (id bigint, iv interval, tz timetz, b bit, "
            "li integer[], lt varchar[], ld date[])"
        )
        con.executemany(
            f"insert into typ2_{side} values (?, ?, ?, ?, ?, ?, ?)", _rows(side)
        )
        con.execute(f"create table huge_{side} (k uhugeint, v varchar)")
        keys = [0, 1, 2**64, 2**127, 2**128 - 2] + ([2**128 - 1] if side == "A" else [])
        con.executemany(
            f"insert into huge_{side} values (cast(? as uhugeint), ?)",
            [(str(k), "x" if (k != 2**127 or side == "A") else "y") for k in keys],
        )
    con.close()
    d = open_duckdb(path, side="B")
    d.path = path  # type: ignore[attr-defined]
    yield d
    d.close()


def test_duckdb_to_duckdb(duck):
    """Same engine: the planted rows, nothing else - COV-7's pair included,
    which the old varchar rendering called identical."""
    a = open_duckdb(duck.path, side="A")
    try:
        _check(diff(a, duck, "typ2_A", "typ2_B", "id"), COLUMNS)
    finally:
        a.close()


def test_duckdb_uhugeint_key_is_hashed_and_exact(duck):
    """Keys up to 2^128 - 1: the missing top key and the changed 2^127 row
    are found, by their exact decimal keys."""
    a = open_duckdb(duck.path, side="A")
    try:
        result = diff(a, duck, "huge_A", "huge_B", "k")
    finally:
        a.close()
    found = {(d.key, d.kind) for d in result.diffs}
    assert found == {(str(2**128 - 1), "only_in_a"), (str(2**127), "different")}, found
    assert any("bucketed by a 60-bit hash" in w for w in result.warnings)


def _pg_array(items: list | None) -> str | None:
    """A PostgreSQL array literal, every element quoted, bound as text."""
    if items is None:
        return None

    def one(x) -> str:
        """One element, quoted and escaped; NULL bare."""
        if x is None:
            return "NULL"
        text = str(x)
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    return "{" + ",".join(one(x) for x in items) + "}"


@pytest.fixture(scope="module")
def pg(pg_url):
    """Side A on PostgreSQL, in `parity_types`: the same columns, plus inet,
    cidr and name, and a table holding a two-dimensional array."""
    import psycopg

    con = psycopg.connect(pg_url, autocommit=True)
    try:
        con.execute("create schema if not exists parity_types")
        for side in ("a", "b"):
            con.execute(f"drop table if exists parity_types.typ2_{side}")
            con.execute(
                f"create table parity_types.typ2_{side} (id bigint, iv interval, "
                "tz timetz, b bit varying, li integer[], lt text[], ld date[], "
                "ip inet, cd cidr, nm name)"
            )
            with con.cursor() as cur:
                cur.executemany(
                    f"insert into parity_types.typ2_{side} values "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            i,
                            iv,
                            tz,
                            b,
                            _pg_array(li),
                            _pg_array(lt),
                            _pg_array(ld),
                            # 10.0.0.1/32 against 10.0.0.1/8 at key 130; the
                            # IPv4-mapped against the IPv4-compatible at 140.
                            "10.0.0.1/8"
                            if (i, side) == (130, "b")
                            else "::1.2.3.4"
                            if (i, side) == (140, "b")
                            else "::ffff:1.2.3.4"
                            if i == 140
                            else f"10.0.{i % 256}.1",
                            f"10.{i % 256}.0.0/16",
                            f"user_{i}",
                        )
                        for i, iv, tz, b, li, lt, ld in _rows(side.upper())
                    ],
                )
        for side, rows in SHAPES.items():
            con.execute(f"drop table if exists parity_types.typ2_dims_{side}")
            con.execute(
                f"create table parity_types.typ2_dims_{side} "
                "(id bigint, li integer[], lt text[], lb boolean[])"
            )
            with con.cursor() as cur:
                cur.executemany(
                    f"insert into parity_types.typ2_dims_{side} values (%s, %s, %s, %s)",
                    rows,
                )
    finally:
        con.close()
    d = open_pg(pg_url, side="A")
    yield d
    d.close()
    con = psycopg.connect(pg_url, autocommit=True)
    for t in ("typ2_a", "typ2_b", "typ2_dims_a", "typ2_dims_b"):
        con.execute(f"drop table if exists parity_types.{t}")
    con.close()


#: PostgreSQL arrays whose shape is part of the value: (id, int[], text[],
#: bool[]) as text, per side. Every B row differs from A in its shape only.
SHAPES = {
    "a": [
        (1, "{{1,2},{3,4}}", "{{a,b}}", "{{t}}"),
        (2, "[0:1]={1,2}", "[0:0]={x}", "[2:3]={t,NULL}"),
        (3, "{{1,2},{3,4}}", '{{a,NULL},{"","x,y"}}', "{t,f}"),
        (4, "[1:1][1:4]={{1,2,3,4}}", "{a}", "{{t,f},{f,t}}"),
    ],
    "b": [
        (1, "{1,2,3,4}", "{a,b}", "{t}"),  # flattened
        (2, "{1,2}", "{x}", "{t,NULL}"),  # lower bound 1
        (3, "{{1,2},{3,4}}", '{{a,NULL},{"","x,y"}}', "{t,f}"),  # the same
        (4, "{{1,2},{3,4}}", "{a}", "{t,f,f,t}"),  # 1x4 against 2x2
    ],
}


@pytest.mark.postgres
def test_postgres_to_duckdb(pg, duck):
    """Across engines: intervals, offsets, bit strings and lists compare
    exactly - the same text on both engines, so only the planted rows."""
    result = diff(
        pg, duck, "parity_types.typ2_a", "typ2_B", "id", exclude=["ip", "cd", "nm"]
    )
    _check(result, COLUMNS)


@pytest.mark.postgres
def test_postgres_to_postgres_inet_and_name(pg, pg_url):
    """inet keeps its prefix and its IPv6 form; cidr and name compare too."""
    b = open_pg(pg_url, side="B")
    try:
        result = diff(pg, b, "parity_types.typ2_a", "parity_types.typ2_b", "id")
    finally:
        b.close()
    planted = {**PLANTED, 130: "ip", 140: "ip"}
    _check(result, COLUMNS | {"ip", "cd", "nm"}, planted)


@pytest.mark.postgres
def test_postgres_array_shape_is_part_of_the_value(pg, pg_url):
    """A 2x2 array is not its four elements flattened, nor a 1x4 one, and
    `[0:1]={1,2}` is not `{1,2}`: `unnest` would flatten all of them alike,
    so the dimensions are rendered too - and read by the sample check from
    the array's text, since psycopg drops lower bounds."""
    b = open_pg(pg_url, side="B")
    try:
        result = diff(pg, b, "parity_types.typ2_dims_a", "parity_types.typ2_dims_b", "id")
    finally:
        b.close()
    found = {d.key: sorted(d.columns) for d in result.diffs}
    assert found == {1: ["lb", "li", "lt"], 2: ["lb", "li", "lt"], 4: ["lb", "li"]}, found
    v = result.verification
    assert v is not None and not v.failures and not v.error, v
    assert set(v.checked) == {"li", "lt", "lb"}, v


@pytest.mark.mysql
@pytest.mark.postgres
def test_mysql_bits_against_postgres(mysql_url, pg_url):
    """MySQL BIT(8) against PostgreSQL bit(8): digits at full width."""
    import psycopg

    w = open_mysql(mysql_url, side="W")
    cur = mysql_rw_cursor(w)
    values = [(i, format(i * 37 % 256, "08b")) for i in range(1, 60)]
    try:
        cur.execute("drop table if exists typ2_bits")
        cur.execute("create table typ2_bits (id bigint, b bit(8))")
        cur.executemany(
            "insert into typ2_bits values (%s, %s)",
            [(i, int(v, 2).to_bytes(1, "big")) for i, v in values],
        )
        w._conn.commit()
        pgc = psycopg.connect(pg_url, autocommit=True)
        pgc.execute("create schema if not exists parity_types")
        pgc.execute("drop table if exists parity_types.typ2_bits")
        pgc.execute("create table parity_types.typ2_bits (id bigint, b bit(8))")
        with pgc.cursor() as pcur:
            pcur.executemany(
                "insert into parity_types.typ2_bits values (%s, %s)",
                [(i, "11111111" if i == 7 else v) for i, v in values],
            )
        a, b = open_mysql(mysql_url, side="A"), open_pg(pg_url, side="B")
        try:
            result = diff(a, b, "typ2_bits", "parity_types.typ2_bits", "id")
        finally:
            a.close()
            b.close()
        _check(result, {"b"}, {7: "b"})
    finally:
        cur.execute("drop table if exists typ2_bits")
        w._conn.commit()
        cur.close()
        w.close()
        pgc = psycopg.connect(pg_url, autocommit=True)
        pgc.execute("drop table if exists parity_types.typ2_bits")
        pgc.close()


#: sql_variant rows: (id, side A's value SQL, side B's) - the same unless planted.
VARIANT_ROWS = [
    (1, "cast(1 as int)", "cast(1 as bigint)"),  # planted: int 1 is not bigint 1
    (2, "cast(1.5 as float)", "cast(1.5 as decimal(2,1))"),  # planted
    (3, "cast(N'1' as nvarchar(5))", "cast('1' as varchar(5))"),  # planted
    (
        4,
        "cast('2024-01-02 03:04:05.1234567' as datetime2(7))",
        "cast('2024-01-02 03:04:05.1234568' as datetime2(7))",
    ),  # planted
    (5, "cast(-1e-300 as float)", "cast(-1e-300 as float)"),  # raised before (COV-8)
    (6, "-cast(0 as float)", "cast(0 as float)"),  # -0 is 0 in SQL Server
    (
        7,
        "cast('2024-01-02 03:04:05.007' as datetime)",
        "cast('2024-01-02 03:04:05.007' as datetime)",
    ),
    (8, "cast(0x00ab as varbinary(5))", "cast(0x00ab00 as varbinary(5))"),  # planted
    (9, "cast(2.25 as money)", "cast(2.25 as money)"),
    (10, "NULL", "cast(N'' as nvarchar(5))"),  # planted: NULL is not ''
]


@pytest.mark.mssql
def test_mssql_variant_to_variant(mssql_url):
    """sql_variant against itself: base type and exact value, nothing else."""
    from urllib.parse import urlparse, urlunparse

    import mssql_python

    from parity.dialects.base import get_dialect
    from parity.dialects.mssql_dialect import parse_mssql_url

    odbc, _ = parse_mssql_url(mssql_url)
    master = mssql_python.connect(odbc, autocommit=True)
    cur = master.cursor()
    cur.execute("if db_id('typ_rules2_db') is null create database typ_rules2_db")
    cur.execute("alter database typ_rules2_db set allow_snapshot_isolation on")
    cur.close()
    url = urlunparse(urlparse(mssql_url)._replace(path="/typ_rules2_db"))
    con = mssql_python.connect(parse_mssql_url(url)[0], autocommit=True)
    try:
        c = con.cursor()
        for side, pick in (("a", 1), ("b", 2)):
            c.execute(f"drop table if exists dbo.var_{side}")
            c.execute(f"create table dbo.var_{side} (id bigint, v sql_variant)")
            for row in VARIANT_ROWS:
                c.execute(f"insert into dbo.var_{side} values ({row[0]}, {row[pick]})")
        c.close()
        a, b = get_dialect(url, side="A"), get_dialect(url, side="B")
        try:
            result = diff(a, b, "dbo.var_a", "dbo.var_b", "id")
        finally:
            a.close()
            b.close()
        _check(result, {"v"}, {1: "v", 2: "v", 3: "v", 4: "v", 8: "v", 10: "v"})
    finally:
        con.close()
        cur = master.cursor()
        cur.execute(
            "alter database typ_rules2_db set single_user with rollback immediate; "
            "drop database typ_rules2_db"
        )
        cur.close()
        master.close()
