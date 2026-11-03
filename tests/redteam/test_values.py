r"""Red team: adversarial *values* and canonical-text *collisions*.

The correctness contract is "two rows are equal iff their canonical text is
byte-identical". This file attacks both halves of that "iff": values that are
different but render the same text (a false match), and values that are the
same but render different text (a false difference).

Every confirmed bug is a minimal reproduction marked
`xfail(strict=True, reason="RT-A<n>: ...")`, so the suite stays green while the
bug stands and turns red the moment it is fixed (then drop the marker). Tests
without the marker are attacks the tool withstood; each one also plants a real
difference and asserts it is found, so none of them can pass by saying
"identical" to everything (CLAUDE.md section 8).

Engines skip, never fail, when unreachable. Tables are prefixed `rt_val_`.
"""

from __future__ import annotations

import io
import random

import pytest
from fakes import DictTable, FakeDialect

from parity.cli import render_human
from parity.dialects.base import canonical_float_text
from parity.engine import diff
from parity.types import Column, LogicalType

from .engines import (
    BigQueryEngine,
    DuckDBEngine,
    Engine,
    MSSQLEngine,
    MySQLEngine,
    PostgresEngine,
)

SEP = "\x1f"
SENTINEL = "\\N"

#: Plain string-column DDL per engine. SQL Server needs nvarchar for Unicode.
TEXT = {
    "duckdb": "varchar",
    "postgres": "varchar(40)",
    "mysql": "varchar(40)",
    "mssql": "nvarchar(40)",
}


@pytest.fixture(scope="module")
def engines(tmp_path_factory):
    """`get(name, slot)`: one lazily built engine per (engine, side) pair."""
    made: dict[tuple[str, str], Engine] = {}
    tmp = str(tmp_path_factory.mktemp("rt_val"))
    factory = {
        "duckdb": lambda: DuckDBEngine(tmp),
        "postgres": PostgresEngine,
        "mysql": MySQLEngine,
        "mssql": MSSQLEngine,
        "bigquery": BigQueryEngine,
    }

    def get(name: str, slot: str) -> Engine:
        if (name, slot) not in made:
            made[(name, slot)] = factory[name]()
        return made[(name, slot)]

    yield get
    for e in made.values():
        e.drop_all()


def run_pair(engines, name, a, b, key="id", **kw):
    """Build table A and table B, diff them with parity, return the result.

    `a` and `b` are `(engine, ddl, rows, raw)` tuples; rows are bound as
    parameters, `raw` are literal insert statements with `{t}` for the table.
    """
    ea, eb = engines(a[0], "A"), engines(b[0], "B")
    ta = ea.build(f"rt_val_{name}_a", a[1], *a[2:])
    tb = eb.build(f"rt_val_{name}_b", b[1], *b[2:])
    da, db = ea.open("A"), eb.open("B")
    try:
        return diff(da, db, ea.ref(ta), eb.ref(tb), key=key, **kw)
    finally:
        da.close()
        db.close()


def found(result) -> list[tuple]:
    """The differences as (key, kind) pairs."""
    return [(d.key, d.kind) for d in result.diffs]


SAME_ENGINE = ["duckdb", "postgres", "mysql", "mssql"]


# =====================================================================
# RT-A1  The field separator inside a value smears two columns into one
# =====================================================================


@pytest.mark.parametrize(
    ("ea", "eb"),
    [(e, e) for e in SAME_ENGINE] + [("postgres", "mssql"), ("mysql", "duckdb")],
)
@pytest.mark.xfail(
    strict=True,
    reason="RT-A1: chr(31) inside a string is not escaped, so ('a\\x1fb','c') "
    "and ('a','b\\x1fc') render the same row text - a false match",
)
def test_rt_a1_separator_inside_a_value_is_a_false_match(engines, ea, eb):
    r"""Row text is `concat_ws(chr(31), ...)` with no escaping, so moving a
    chr(31) from one column to the next leaves the joined bytes - and the hash
    - unchanged. Both columns changed; the checksum cannot see it.

    Values: A = (1, 'a\x1fb', 'c'), B = (1, 'a', 'b\x1fc').
    """
    r = run_pair(
        engines,
        f"a1_{ea}_{eb}",
        (ea, f"id int, x {TEXT[ea]}, y {TEXT[ea]}", [(1, f"a{SEP}b", "c")]),
        (eb, f"id int, x {TEXT[eb]}, y {TEXT[eb]}", [(1, "a", f"b{SEP}c")]),
    )
    assert found(r) == [(1, "different")]


@pytest.mark.bigquery
@pytest.mark.xfail(strict=True, reason="RT-A1: separator collision, BigQuery <-> DuckDB")
def test_rt_a1_separator_collision_on_bigquery(engines):
    r = run_pair(
        engines,
        "a1_bq",
        (
            "bigquery",
            "id int64, x string, y string",
            (),
            ["insert into {t} values (1, concat('a', chr(31), 'b'), 'c')"],
        ),
        ("duckdb", "id bigint, x varchar, y varchar", [(1, "a", f"b{SEP}c")]),
    )
    assert found(r) == [(1, "different")]


# =====================================================================
# RT-A2  A string that literally reads \N is the NULL sentinel
# =====================================================================


@pytest.mark.parametrize(
    ("ea", "eb"),
    [(e, e) for e in SAME_ENGINE] + [("mysql", "mssql"), ("duckdb", "mssql")],
)
@pytest.mark.xfail(
    strict=True,
    reason="RT-A2: the two-character string '\\N' renders exactly like SQL NULL "
    "- a NULL-to-'\\N' change is a false match",
)
def test_rt_a2_backslash_n_string_equals_null(engines, ea, eb):
    r"""NULL renders as the literal `\N` and string data is not escaped, so a
    real two-character value `\N` (a common NULL spelling in MySQL dumps and
    PostgreSQL COPY files - exactly what a botched migration leaves behind) is
    indistinguishable from SQL NULL.

    Values: A = (1, '\N'), B = (1, NULL).
    """
    r = run_pair(
        engines,
        f"a2_{ea}_{eb}",
        (ea, f"id int, x {TEXT[ea]}", [(1, SENTINEL)]),
        (eb, f"id int, x {TEXT[eb]}", [(1, None)]),
    )
    assert found(r) == [(1, "different")]


@pytest.mark.bigquery
@pytest.mark.xfail(strict=True, reason="RT-A2: '\\N' string vs NULL, BigQuery <-> DuckDB")
def test_rt_a2_backslash_n_string_on_bigquery(engines):
    r = run_pair(
        engines,
        "a2_bq",
        ("bigquery", "id int64, x string", (), ["insert into {t} values (1, r'\\N')"]),
        ("duckdb", "id bigint, x varchar", [(1, None)]),
    )
    assert found(r) == [(1, "different")]


# =====================================================================
# RT-A3 / RT-A4  The same two collisions in a *hashed key's identity*
# =====================================================================


@pytest.mark.parametrize(("ea", "eb"), [("postgres", "duckdb"), ("mysql", "mssql")])
@pytest.mark.xfail(
    strict=True,
    reason="RT-A3: a NULL in a hashed (text) key passes the NULL-key check and "
    "matches a real '\\N' key on the other side",
)
def test_rt_a3_null_text_key_matches_backslash_n_key(engines, ea, eb):
    r"""For a hashed key the identity is coalesced text, so `count(identity)`
    never sees a NULL and the NULL-key refusal never fires. A row keyed NULL
    is then matched to a row keyed `'\N'` - two different rows reported as one.

    Values: A = (k='\N', x=1), B = (k=NULL, x=1).
    """
    r = run_pair(
        engines,
        f"a3_{ea}_{eb}",
        (ea, f"k {TEXT[ea]}, x int", [(SENTINEL, 1)]),
        (eb, f"k {TEXT[eb]}, x int", [(None, 1)]),
        key="k",
    )
    assert not r.identical


@pytest.mark.parametrize(
    ("ea", "eb"), [("postgres", "mysql"), ("duckdb", "mssql"), ("duckdb", "duckdb")]
)
@pytest.mark.xfail(
    strict=True,
    reason="RT-A4: composite key ('a\\x1fb','c') and ('a','b\\x1fc') have the same "
    "identity text, so two different keys are matched as one row",
)
def test_rt_a4_composite_key_separator_collision(engines, ea, eb):
    r"""The engine's own warning promises "a hash collision cannot merge two
    rows" - but the *identity* is the unescaped joined text, so it can.

    Values: A = (k1='a\x1fb', k2='c', x=1), B = (k1='a', k2='b\x1fc', x=1).
    """
    r = run_pair(
        engines,
        f"a4_{ea}_{eb}",
        (ea, f"k1 {TEXT[ea]}, k2 {TEXT[ea]}, x int", [(f"a{SEP}b", "c", 1)]),
        (eb, f"k1 {TEXT[eb]}, k2 {TEXT[eb]}, x int", [("a", f"b{SEP}c", 1)]),
        key=["k1", "k2"],
    )
    assert len(r.diffs) == 2  # one only_in_a, one only_in_b


@pytest.mark.xfail(
    strict=True,
    reason="RT-A4: the same collision inside one table refuses a unique "
    "composite key as 'not unique'",
)
def test_rt_a4_composite_key_collision_refused_as_duplicate(engines):
    rows = [(f"a{SEP}b", "c", 1), ("a", f"b{SEP}c", 2)]
    ddl = "k1 varchar, k2 varchar, x int"
    r = run_pair(
        engines,
        "a4_dup",
        ("duckdb", ddl, rows),
        ("duckdb", ddl, [*rows[:1], (rows[1][0], rows[1][1], 99)]),
        key=["k1", "k2"],
    )
    assert len(r.diffs) == 1


@pytest.mark.xfail(
    strict=True,
    reason="RT-A3: composite key with a NULL component matches the literal '\\N'",
)
def test_rt_a3_composite_key_null_component(engines):
    r = run_pair(
        engines,
        "a3_comp",
        ("duckdb", "k1 varchar, k2 varchar, x int", [("a", None, 1)]),
        ("postgres", "k1 varchar(9), k2 varchar(9), x int", [("a", SENTINEL, 1)]),
        key=["k1", "k2"],
    )
    assert not r.identical


# =====================================================================
# RT-A5 / RT-A6  PostgreSQL dates: infinity is NULL, BC is AD
# =====================================================================


@pytest.mark.parametrize("typ", ["date", "timestamp", "timestamptz"])
@pytest.mark.xfail(
    strict=True,
    reason="RT-A5: PostgreSQL to_char() returns NULL for +/-infinity, so "
    "'infinity', '-infinity' and NULL all render '\\N'",
)
def test_rt_a5_pg_infinity_equals_minus_infinity(engines, typ):
    r = run_pair(
        engines,
        f"a5_{typ}",
        ("postgres", f"id int, d {typ}", (), ["insert into {t} values (1, 'infinity')"]),
        ("postgres", f"id int, d {typ}", (), ["insert into {t} values (1, '-infinity')"]),
    )
    assert found(r) == [(1, "different")]


@pytest.mark.xfail(
    strict=True,
    reason="RT-A5: PostgreSQL 'infinity' timestamp matches a NULL on DuckDB",
)
def test_rt_a5_pg_infinity_matches_null_on_duckdb(engines):
    r = run_pair(
        engines,
        "a5_null",
        (
            "postgres",
            "id int, d timestamp",
            (),
            ["insert into {t} values (1, 'infinity')"],
        ),
        ("duckdb", "id int, d timestamp", [(1, None)]),
    )
    assert found(r) == [(1, "different")]


@pytest.mark.xfail(
    strict=True,
    reason="RT-A5: the same 'infinity' reads '\\N' on PostgreSQL and 'infinity' "
    "on DuckDB - a false difference",
)
def test_rt_a5_infinity_on_both_sides_is_a_false_difference(engines):
    ins = ["insert into {t} values (1, 'infinity'), (2, '2024-01-01')"]
    r = run_pair(
        engines,
        "a5_both",
        ("postgres", "id int, d date", (), ins),
        (
            "duckdb",
            "id int, d date",
            (),
            ["insert into {t} values (1, 'infinity'), (2, '2024-01-02')"],
        ),
    )
    assert found(r) == [(2, "different")]


@pytest.mark.parametrize("typ", ["date", "timestamp", "timestamptz"])
@pytest.mark.xfail(
    strict=True,
    reason="RT-A6: PostgreSQL 'YYYY' drops the era, so 44 BC renders exactly like AD 44",
)
def test_rt_a6_pg_bc_equals_ad(engines, typ):
    v = "0044-03-15" if typ == "date" else "0044-03-15 12:00:00"
    if typ == "timestamptz":
        v += "+00"
    r = run_pair(
        engines,
        f"a6_{typ}",
        ("postgres", f"id int, d {typ}", (), [f"insert into {{t}} values (1, '{v} BC')"]),
        ("postgres", f"id int, d {typ}", (), [f"insert into {{t}} values (1, '{v}')"]),
    )
    assert found(r) == [(1, "different")]


@pytest.mark.xfail(
    strict=True,
    reason="RT-A6: PostgreSQL 44 BC matches DuckDB AD 44 (false match)",
)
def test_rt_a6_pg_bc_matches_duckdb_ad(engines):
    r = run_pair(
        engines,
        "a6_duck",
        (
            "postgres",
            "id int, d date",
            (),
            ["insert into {t} values (1, '0044-03-15 BC')"],
        ),
        ("duckdb", "id int, d date", (), ["insert into {t} values (1, '0044-03-15')"]),
    )
    assert found(r) == [(1, "different")]


@pytest.mark.xfail(
    strict=True,
    reason="RT-A6: the same BC date renders '0044-03-15' on PostgreSQL and "
    "'-43-03-15' on DuckDB - a false difference",
)
def test_rt_a6_bc_on_both_sides_is_a_false_difference(engines):
    r = run_pair(
        engines,
        "a6_both",
        (
            "postgres",
            "id int, d date",
            (),
            ["insert into {t} values (1, '0044-03-15 BC'), (2, '2024-01-01')"],
        ),
        (
            "duckdb",
            "id int, d date",
            (),
            ["insert into {t} values (1, '0044-03-15 (BC)'), (2, '2024-01-02')"],
        ),
    )
    assert found(r) == [(2, "different")]


# =====================================================================
# RT-A7  DuckDB TIMESTAMP_NS is silently compared at microseconds
# =====================================================================


def _mentions_precision(warnings: list[str]) -> bool:
    return any("nanosecond" in w.lower() or "microsecond" in w.lower() for w in warnings)


@pytest.mark.parametrize("other", ["duckdb", "postgres"])
@pytest.mark.xfail(
    strict=True,
    reason="RT-A7: DuckDB TIMESTAMP_NS loses its nanoseconds in strftime('%f') "
    "and, unlike SQL Server's datetime2(7), no caveat says so",
)
def test_rt_a7_duckdb_nanoseconds_vanish_silently(engines, other):
    """SQL Server's dialect warns that its 100 ns ticks are truncated; DuckDB's
    TIMESTAMP_NS loses three digits with no word in the output (section 8).

    Values: A = 2024-01-01 00:00:00.000000001 (TIMESTAMP_NS),
            B = 2024-01-01 00:00:00.000000999 (or a PostgreSQL .000000).
    """
    b = (
        ("duckdb", "id int, t timestamp_ns", [(1, "2024-01-01 00:00:00.000000999")])
        if other == "duckdb"
        else ("postgres", "id int, t timestamp", [(1, "2024-01-01 00:00:00")])
    )
    r = run_pair(
        engines,
        f"a7_{other}",
        ("duckdb", "id int, t timestamp_ns", [(1, "2024-01-01 00:00:00.000000001")]),
        b,
    )
    assert not r.identical or _mentions_precision(r.warnings)


# =====================================================================
# RT-A8  MySQL bytes that are not valid UTF-8 render as NULL
# =====================================================================


@pytest.mark.parametrize(
    ("typ", "va", "vb"),
    [
        ("varbinary(8)", "0xff", "0xfe"),
        ("varbinary(8)", "0xff", "null"),
        ("blob", "0x8081", "0x5c4e"),  # 0x5c4e is the bytes of '\N'
        ("binary(2)", "0xc0c1", "0xf5f6"),
        ("bit(8)", "b'10000000'", "b'11111111'"),
    ],
)
def test_rt_a8_mysql_binary_not_utf8_is_null(engines, typ, va, vb):
    """The 'unmapped types' warning says raw-text comparison "may differ across
    engines" - it does not say two *different* values on the *same* engine will
    compare equal, which is what happens. Values as in the parameters.
    """
    r = run_pair(
        engines,
        "a8",
        ("mysql", f"id int, b {typ}", (), [f"insert into {{t}} values (1, {va})"]),
        ("mysql", f"id int, b {typ}", (), [f"insert into {{t}} values (1, {vb})"]),
    )
    assert found(r) == [(1, "different")]


# =====================================================================
# RT-A9 / RT-A10  A lossy fetch turns a proven checksum mismatch into "identical"
# =====================================================================


@pytest.mark.parametrize("other", ["mssql", "duckdb"])
@pytest.mark.xfail(
    strict=True,
    reason="RT-A9: mssql-python drops trailing NUL characters from nvarchar(max) "
    "results, so 'a\\x00' fetches as 'a' and a real difference is discarded",
)
def test_rt_a9_mssql_trailing_nul_dropped_on_fetch(engines, other):
    """The checksums *do* differ (the server hashes 'a\\x00' correctly), the
    range is downloaded, and then the Python comparison of the fetched text
    sees 'a' == 'a'. A NUL in the middle survives; only trailing ones vanish.

    Values: A = (1, N'a' + nchar(0)) on SQL Server, B = (1, 'a').
    """
    b = (
        ("mssql", "id int, s nvarchar(5)", (), ["insert into {t} values (1, N'a')"])
        if other == "mssql"
        else ("duckdb", "id int, s varchar", [(1, "a")])
    )
    r = run_pair(
        engines,
        f"a9_{other}",
        (
            "mssql",
            "id int, s nvarchar(5)",
            (),
            ["insert into {t} values (1, N'a' + nchar(0))"],
        ),
        b,
    )
    assert found(r) == [(1, "different")]


def test_rt_a10_checksum_mismatch_with_equal_rows_reads_identical():
    """The engine-level root of RT-A9. Any lossy step between the server's hash
    and Python's `!=` - a driver dropping bytes, a decode replacing them - turns
    a difference the checksum *proved* into a silent "identical". The engine
    holds the evidence (a mismatched bucket, rows downloaded) and discards it.

    Modelled with a side whose fetch lower-cases text: its checksum sees 'B',
    its fetch says 'b'.
    """

    class LossyFetch(FakeDialect):
        def fetch_range(self, table, key, columns, lo, hi):
            out = super().fetch_range(table, key, columns, lo, hi)
            return {k: tuple(v.lower() for v in row) for k, row in out.items()}

    cols = [Column("v", LogicalType.STRING, "varchar")]
    a = FakeDialect(DictTable(cols, {1: ("a",), 2: ("b",)}), side="A")
    b = LossyFetch(DictTable(cols, {1: ("a",), 2: ("B",)}), side="B")
    r = diff(a, b, "a.t", "b.t", "id")
    assert r.stats.rows_downloaded > 0  # the checksum did see it
    assert not r.identical


# =====================================================================
# RT-A11  SQL Server lone surrogates all hash as U+FFFD
# =====================================================================


def test_rt_a11_mssql_lone_surrogates_collide(engines):
    """Values: A = nchar(55296) (U+D800), B = nchar(55297) (U+D801)."""
    r = run_pair(
        engines,
        "a11",
        (
            "mssql",
            "id int, s nvarchar(5)",
            (),
            ["insert into {t} values (1, nchar(55296))"],
        ),
        (
            "mssql",
            "id int, s nvarchar(5)",
            (),
            ["insert into {t} values (1, nchar(55297))"],
        ),
    )
    assert found(r) == [(1, "different")]


def test_rt_a11_mssql_lone_surrogate_matches_replacement_char(engines):
    r = run_pair(
        engines,
        "a11_duck",
        (
            "mssql",
            "id int, s nvarchar(5)",
            (),
            ["insert into {t} values (1, nchar(56320))"],
        ),
        ("duckdb", "id int, s varchar", [(1, "\ufffd")]),
    )
    assert found(r) == [(1, "different")]


# =====================================================================
# RT-A12  DuckDB UNION loses its tag
# =====================================================================


@pytest.mark.xfail(
    strict=True,
    reason="RT-A12: DuckDB UNION(n int, s varchar) renders 1::int and '1' as "
    "the same text '1'",
)
def test_rt_a12_duckdb_union_tag_is_lost(engines):
    ddl = "id int, u union(n int, s varchar)"
    r = run_pair(
        engines,
        "a12",
        ("duckdb", ddl, (), ["insert into {t} values (1, 1::int)"]),
        ("duckdb", ddl, (), ["insert into {t} values (1, '1'::varchar)"]),
    )
    assert found(r) == [(1, "different")]


# =====================================================================
# RT-A13  MySQL's collation merges distinct text keys
# =====================================================================


@pytest.mark.parametrize("pair", [("a", "A"), ("\u00df", "ss"), ("e", "\u00e9")])
def test_rt_a13_mysql_collation_refuses_unique_text_key(engines, pair):
    """SQL Server's dialect counts uniqueness on a digest for exactly this
    reason (`count_distinct_key`); MySQL inherits the collation-equal default.
    Loud, not silent - but it refuses a correct table outright.
    """
    rows_a = [(pair[0], 1), (pair[1], 2)]
    rows_b = [(pair[0], 1), (pair[1], 3)]
    r = run_pair(
        engines,
        "a13",
        ("mysql", "k varchar(20), x int", rows_a),
        ("duckdb", "k varchar, x int", rows_b),
        key="k",
    )
    assert found(r) == [(pair[1], "different")]


# =====================================================================
# RT-A14  An integer key migrated to DECIMAL(n,0)/FLOAT
# =====================================================================


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (("postgres", "id int, x int"), ("duckdb", "id decimal(10,0), x int")),
        (("mysql", "id int, x int"), ("mssql", "id float, x int")),
    ],
)
def test_rt_a14_integer_key_vs_decimal_key(engines, a, b):
    """Oracle NUMBER, `numeric(10,0)` and float ids are ordinary migration
    outcomes. Non-key columns of mismatched type get an explicit warning; the
    key gets "non-integer key (integer)" - which names the wrong side's type
    and does not say why every single row is missing from both sides.

    Values: A = id int {1, 2}, B = id decimal(10,0) / float {1, 2}, x equal
    except row 2.
    """
    r = run_pair(
        engines,
        f"a14_{a[0]}",
        (*a, [(1, 1), (2, 2)]),
        (*b, [(1, 1), (2, 3)]),
    )
    sane = len(r.diffs) == 1 or any(
        "key" in w and ("differ" in w or "type" in w) and "hash" not in w
        for w in r.warnings
    )
    assert sane, (r.diffs, r.warnings)


# =====================================================================
# RT-A15  The human report prints a real '\N' string as NULL
# =====================================================================


@pytest.mark.xfail(
    strict=True,
    reason="RT-A15: the CLI renders the value '\\N' as NULL, so a '\\N' -> '' "
    "change is reported as NULL -> ''",
)
def test_rt_a15_cli_prints_backslash_n_as_null(engines):
    r = run_pair(
        engines,
        "a15",
        ("duckdb", "id int, x varchar", [(1, SENTINEL)]),
        ("duckdb", "id int, x varchar", [(1, "")]),
    )
    assert found(r) == [(1, "different")]  # the tool does find it...
    out = io.StringIO()
    render_human(r, out)
    assert "NULL" not in out.getvalue()  # ...but must not call it NULL


# =====================================================================
# Attacks the tool withstood. Each also plants a difference it must find.
# =====================================================================


@pytest.mark.parametrize(
    ("label", "ea", "va", "eb", "vb"),
    [
        ("nfc_nfd", "postgres", "\u00e9", "duckdb", "e\u0301"),
        ("homoglyph", "mysql", "\u0430", "postgres", "a"),  # Cyrillic a
        ("zwsp", "mysql", "ab\u200b", "mssql", "ab"),
        (
            "zwj",
            "duckdb",
            "\U0001f468\u200d\U0001f469",
            "postgres",
            "\U0001f468\U0001f469",
        ),
        ("trailing_space", "mysql", "ab ", "mssql", "ab"),
        ("crlf", "postgres", "a\r\nb", "mysql", "a\nb"),
        ("bom", "duckdb", "\ufeffa", "mssql", "a"),
        ("astral", "mysql", "\U0001f600", "mssql", "\U0001f601"),
        ("fullwidth", "postgres", "\uff11", "mssql", "1"),
        ("empty_vs_space", "duckdb", "", "mysql", " "),
        ("rtl_mark", "mssql", "\u200fabc", "duckdb", "abc"),
        ("backslash", "postgres", "a\\b", "mysql", "a\\\\b"),
        ("quote", "mysql", "it's", "mssql", "it''s"),
        ("sentinel_vs_empty", "postgres", SENTINEL, "duckdb", ""),
        ("mid_nul", "mysql", "a\x00b", "duckdb", "a\x00c"),
    ],
)
def test_lookalike_strings_are_reported_different(engines, label, ea, va, eb, vb):
    """Byte-different strings that look alike, or that a careless normaliser
    would fold, must be reported - with nothing else."""
    if "\x00" in va and ea == "postgres":
        pytest.skip("PostgreSQL text cannot hold NUL")
    r = run_pair(
        engines,
        f"ok_{label}",
        (ea, f"id int, s {TEXT[ea]}", [(1, va), (2, "same")]),
        (eb, f"id int, s {TEXT[eb]}", [(1, vb), (2, "same")]),
    )
    assert found(r) == [(1, "different")]


@pytest.mark.parametrize(
    ("ea", "eb", "value"),
    [
        ("mysql", "mssql", "\U0001f600 \u65e5\u672c \u00fc"),
        ("mssql", "postgres", "\u20ac\u00e9"),  # through a code-page-1252 varchar below
        ("postgres", "duckdb", "\u0130\u0131 i I"),  # Turkish dotted/dotless
    ],
)
def test_equal_unicode_matches_across_engines(engines, ea, eb, value):
    """The same Unicode on both sides matches; a planted change still shows."""
    ta = "varchar(20) collate SQL_Latin1_General_CP1_CI_AS" if ea == "mssql" else TEXT[ea]
    r = run_pair(
        engines,
        f"uni_{ea}_{eb}",
        (ea, f"id int, s {ta}", [(1, value), (2, "x")]),
        (eb, f"id int, s {TEXT[eb]}", [(1, value), (2, "y")]),
    )
    assert found(r) == [(2, "different")]


def test_mssql_char_padding_matches_postgres_char(engines):
    """char(n) padding is stripped on both; a planted change still shows."""
    r = run_pair(
        engines,
        "char_pad",
        ("mssql", "id int, s char(5)", [(1, "ab"), (2, "x")]),
        ("postgres", "id int, s char(5)", [(1, "ab"), (2, "y")]),
    )
    assert found(r) == [(2, "different")]


def test_mssql_mid_string_nul_is_seen(engines):
    """A NUL *inside* a string survives the fetch (contrast RT-A9)."""
    r = run_pair(
        engines,
        "mid_nul_ms",
        (
            "mssql",
            "id int, s nvarchar(5)",
            (),
            ["insert into {t} values (1, N'a' + nchar(0) + N'b')"],
        ),
        (
            "mssql",
            "id int, s nvarchar(5)",
            (),
            ["insert into {t} values (1, N'a' + nchar(0) + N'c')"],
        ),
    )
    assert found(r) == [(1, "different")]


#: Values around zero and the 6-place rounding boundary, per engine DDL.
NUMERIC_DDL = {
    "duckdb": "id int, a decimal(18,8), b decimal(10,7), f double, r real",
    "postgres": "id int, a numeric, b numeric(10,7), f float8, r real",
    "mysql": "id int, a decimal(18,8), b decimal(10,7), f double, r float",
    "mssql": "id int, a decimal(18,8), b decimal(10,7), f float, r real",
}
NUMERIC_ROWS = [
    "(1, -0.0000004, -0.0000004, -0.0000004, -0.0000004)",
    "(2, -0.0000005, -0.0000005, -0.0000005, -0.0000005)",
    "(3, -0.0, -0.0, -0.0, -0.0)",
    "(4, 0.0000005, 1.0000005, 1e-7, 1e-7)",
    "(5, 1.5, 1.50, 1.5, 1.5)",
    "(6, 2.0000005, 2.0000005, 2.0000005, 2.5)",
]


@pytest.mark.parametrize("eb", ["postgres", "mysql", "mssql"])
def test_negative_zero_and_rounding_boundaries_agree(engines, eb):
    """-0, values that round to -0, half-up ties and trailing zeros render the
    same on every engine as on DuckDB; a planted 1e-6 change still shows."""
    planted = "(7, 1.000001, 1, 1, 1)"
    r = run_pair(
        engines,
        f"negzero_{eb}",
        (
            "duckdb",
            NUMERIC_DDL["duckdb"],
            (),
            [
                "insert into {t} values "
                + ", ".join(NUMERIC_ROWS)
                + ", (7, 1.000002, 1, 1, 1)"
            ],
        ),
        (
            eb,
            NUMERIC_DDL[eb],
            (),
            ["insert into {t} values " + ", ".join(NUMERIC_ROWS) + ", " + planted],
        ),
    )
    assert found(r) == [(7, "different")]


def test_duckdb_floats_match_the_spec_off_the_beaten_path():
    """Exponent-notation magnitudes (1e-7..1e-4) and near-half mantissas at
    several scales: DuckDB's FLOAT text equals `canonical_float_text`."""
    import duckdb

    from parity.dialects.duckdb_dialect import DuckDBDialect

    rng = random.Random(7)
    vals = []
    for e in range(-7, 17):
        for _ in range(40):
            m = rng.choice(
                [rng.random() * 10, 5 + rng.choice([-1, 1]) * 10 ** -rng.randint(1, 12)]
            )
            vals += [m * 10.0**e, -m * 10.0**e]
    con = duckdb.connect()
    con.execute("create table t as select unnest(?::double[]) as x", [vals])
    for s in (0, 1, 3, 6, 9):
        d = DuckDBDialect(float_scale=s)
        expr = d.normalize(Column("x", LogicalType.FLOAT, "DOUBLE"))
        rows = con.execute(f"select x, {expr} from t").fetchall()
        bad = [(x, got) for x, got in rows if got != canonical_float_text(x, s)]
        assert not bad, (s, bad[:5])
    con.close()


@pytest.mark.parametrize(
    ("label", "ddl", "va", "vb"),
    [
        ("zero_date", "d date", "'0000-00-00'", "null"),
        ("zero_day", "d date", "'2024-02-00'", "'2024-02-01'"),
        ("invalid_day", "d date", "'2024-02-30'", "'2024-02-31'"),
    ],
)
def test_mysql_zero_and_invalid_dates_are_not_null(engines, label, ddl, va, vb):
    """MySQL's zero and invalid dates render as themselves, never as NULL."""
    mode = "set session sql_mode = 'ALLOW_INVALID_DATES'"
    r = run_pair(
        engines,
        f"mydate_{label}",
        ("mysql", f"id int, {ddl}", (), [mode, f"insert into {{t}} values (1, {va})"]),
        ("mysql", f"id int, {ddl}", (), [mode, f"insert into {{t}} values (1, {vb})"]),
    )
    assert found(r) == [(1, "different")]


@pytest.mark.parametrize(
    ("label", "ddl", "va", "vb"),
    [
        ("blob_backslash", "b blob", "'\\x00'::blob", "'\\x5Cx00'::blob"),
        ("blob_sentinel", "b blob", "'\\x5C\\x4E'::blob", "null"),
        ("list_comma", "l varchar[]", "['a, b']", "['a', 'b']"),
        ("list_null", "l varchar[]", "[null]", "['NULL']"),
        ("struct_null", "s struct(a varchar)", "{'a': null}", "{'a': 'NULL'}"),
        (
            "map_quote",
            "m map(varchar, varchar)",
            "map {'k': 'v, k2=v2'}",
            "map {'k': 'v', 'k2': 'v2'}",
        ),
    ],
)
def test_duckdb_nested_and_binary_text_does_not_collide(engines, label, ddl, va, vb):
    """DuckDB quotes nested strings and hex-escapes blob backslashes, so these
    raw-text renderings stay distinct."""
    va, vb = (v.replace("{", "{{").replace("}", "}}") for v in (va, vb))
    r = run_pair(
        engines,
        f"duck_{label}",
        ("duckdb", f"id int, {ddl}", (), [f"insert into {{t}} values (1, {va})"]),
        ("duckdb", f"id int, {ddl}", (), [f"insert into {{t}} values (1, {vb})"]),
    )
    assert found(r) == [(1, "different")]


def test_postgres_array_text_does_not_collide(engines):
    """PostgreSQL quotes array elements that need it."""
    ins = "insert into {t} values (1, '{{\"a, b\"}}'), (2, '{{NULL}}')"
    r = run_pair(
        engines,
        "pg_arr",
        ("postgres", "id int, l text[]", (), [ins]),
        (
            "postgres",
            "id int, l text[]",
            (),
            ["insert into {t} values (1, '{{a,b}}'), (2, '{{\"NULL\"}}')"],
        ),
    )
    assert found(r) == [(1, "different"), (2, "different")]


@pytest.mark.parametrize(
    ("va", "vb"),
    [
        ("'0005-01-01 00:00:00'", "'0005-01-01 00:00:00.000001'"),
        ("'9999-12-31 23:59:59.999999'", "'9999-12-31 23:59:59.999998'"),
    ],
)
def test_extreme_years_agree_across_engines(engines, va, vb):
    """Years below 1000 and 9999 render alike on PostgreSQL, MySQL, SQL Server
    and DuckDB; a microsecond change at the edge still shows."""
    for eb, typ in (
        ("postgres", "timestamp"),
        ("mysql", "datetime(6)"),
        ("mssql", "datetime2(6)"),
    ):
        r = run_pair(
            engines,
            f"years_{eb}",
            (
                "duckdb",
                "id int, t timestamp",
                (),
                [f"insert into {{t}} values (1, {va}), (2, {va})"],
            ),
            (
                eb,
                f"id int, t {typ}",
                (),
                [f"insert into {{t}} values (1, {va}), (2, {vb})"],
            ),
        )
        assert found(r) == [(2, "different")], eb


@pytest.mark.bigquery
def test_bigquery_bytes_are_distinct_and_not_null(engines):
    """BigQuery BYTES render as escaped text: 0xff, 0xfe and NULL all differ."""
    r = run_pair(
        engines,
        "bq_bytes",
        (
            "bigquery",
            "id int64, b bytes",
            (),
            ["insert into {t} values (1, b'\\xff'), (2, b'\\xfe'), (3, b'x')"],
        ),
        (
            "bigquery",
            "id int64, b bytes",
            (),
            ["insert into {t} values (1, b'\\xfe'), (2, NULL), (3, b'x')"],
        ),
    )
    assert found(r) == [(1, "different"), (2, "different")]


# =====================================================================
# Second wave. RT-A16 onwards, then more attacks the tool withstood.
# =====================================================================


# ---------------------------------------------------------------------
# RT-A16  SQL Server sql_variant loses its base type
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("va", "vb"),
    [
        ("cast(1 as int)", "cast(N'1' as nvarchar(5))"),
        ("cast(1.5 as float)", "cast(1.5 as decimal(5,1))"),
    ],
    ids=["int_vs_nvarchar", "float_vs_decimal"],
)
@pytest.mark.xfail(
    strict=True,
    reason="RT-A16: SQL Server sql_variant renders through cast(nvarchar(max)), "
    "which drops the base type, so int 1 and N'1' (or float 1.5 and decimal "
    "1.5) are the same text - a false match on one engine",
)
def test_rt_a16_mssql_sql_variant_type_is_lost(engines, va, vb):
    """The sibling of RT-A12 (DuckDB UNION). The 'unmapped types' warning says
    raw text "may differ across engines"; here two different values on the
    *same* engine compare equal. Row 2 is a planted, ordinary difference.

    Values: A = (1, <va>), B = (1, <vb>) in a `sql_variant` column.
    """
    ddl = "id int, v sql_variant"
    # One statement per row: a multi-row VALUES unifies the types first.
    two = "insert into {t} values (2, cast(N'%s' as nvarchar(5)))"
    r = run_pair(
        engines,
        "a16",
        ("mssql", ddl, (), [f"insert into {{t}} values (1, {va})", two % "x"]),
        ("mssql", ddl, (), [f"insert into {{t}} values (1, {vb})", two % "y"]),
    )
    assert found(r) == [(1, "different"), (2, "different")]


# ---------------------------------------------------------------------
# RT-A17  A MySQL row wider than max_allowed_packet hashes to NULL
# ---------------------------------------------------------------------


def test_rt_a17_mysql_row_over_max_allowed_packet_is_invisible(engines):
    """Each value (34 MB) is well under MySQL 8's 64 MiB default packet; only
    the joined row is over it. The count still matches, so the bucket reads
    clean. MySQL <-> MySQL only: against any other engine the other side's
    sum includes the row, the buckets differ and the per-column fetch sees
    the change. Two rows per side, built server-side with repeat().

    Values: A = (1, 'a'*34e6, 'b'*34e6), B = (1, 'a'*34e6, 'b'*(34e6-1) + 'c');
    row 2 is a planted ordinary difference.
    """
    n = 34_000_000
    ddl = "id int, x longtext, y longtext"
    ea = engines("mysql", "A")
    limit = int(ea.d.query("select @@max_allowed_packet")[0][0])
    if 2 * n < limit:
        pytest.skip(f"max_allowed_packet is {limit}; this needs rows wider than it")
    big_a = f"insert into {{t}} values (1, repeat('a', {n}), repeat('b', {n}))"
    big_b = (
        f"insert into {{t}} values (1, repeat('a', {n}), "
        f"concat(repeat('b', {n - 1}), 'c'))"
    )
    r = run_pair(
        engines,
        "a17",
        ("mysql", ddl, (), [big_a, "insert into {t} values (2, 'p', 'q')"]),
        ("mysql", ddl, (), [big_b, "insert into {t} values (2, 'p', 'r')"]),
    )
    # Pin the mechanism, not just the symptom: the row text really is NULL.
    sep = ea.d.separator_sql()
    (is_null,) = ea.d.query(
        f"select concat_ws({sep}, x, y) is null from `rt_val_a17_a` where id = 1"
    )[0]
    assert is_null == 1
    assert found(r) == [(1, "different"), (2, "different")]


# ---------------------------------------------------------------------
# RT-A18  A key's identity is its *rounded* canonical text
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (
            ("duckdb", "k decimal(18,8), x int", [("1.00000001", 1)]),
            ("postgres", "k numeric(18,8), x int", [("1.00000002", 1)]),
        ),
        (
            ("duckdb", "k double, x int", [(0.1 + 0.2, 1)]),
            ("postgres", "k float8, x int", [(0.3, 1)]),
        ),
        (
            ("duckdb", "k timestamp_ns, x int", [("2024-01-01 00:00:00.000000001", 1)]),
            ("duckdb", "k timestamp_ns, x int", [("2024-01-01 00:00:00.000000002", 1)]),
        ),
    ],
    ids=["decimal8", "double", "timestamp_ns"],
)
@pytest.mark.xfail(
    strict=True,
    reason="RT-A18: a hashed key is matched by its canonical text, which rounds "
    "DECIMAL/FLOAT to 6 places and timestamps to microseconds, so two different "
    "keys are matched as one row and the pair reads identical",
)
def test_rt_a18_rounded_key_identity_merges_distinct_keys(engines, a, b, request):
    """'Comparing floats and decimals at 6 decimal places' is printed, but it
    reads as a statement about *values*. Applied to the key it silently
    re-identifies rows: key 1.00000001 on A "is" key 1.00000002 on B. For
    TIMESTAMP_NS nothing at all is printed (compare RT-A7).
    """
    name = request.node.callspec.id
    r = run_pair(engines, f"a18_{name}", a, b, key="k")
    assert not r.identical


@pytest.mark.xfail(
    strict=True,
    reason="RT-A18: two distinct decimal(18,8) keys round to one identity, so a "
    "unique key is refused as 'not unique'",
)
def test_rt_a18_unique_decimal_key_refused_as_duplicate(engines):
    """Loud, not silent - but the message tells the user something false about
    their own table. Values: keys 1.00000001 and 1.00000002, x differs on row 2.
    """
    r = run_pair(
        engines,
        "a18_dup",
        ("duckdb", "k decimal(18,8), x int", [("1.00000001", 1), ("1.00000002", 2)]),
        ("postgres", "k numeric(18,8), x int", [("1.00000001", 1), ("1.00000002", 3)]),
        key="k",
    )
    assert len(r.diffs) == 1


# ---------------------------------------------------------------------
# RT-A19  A DuckDB HUGEINT key wider than INT128/2 crashes the walk
# ---------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="RT-A19: HUGEINT keys are bisected unhashed, and `wide_int` is hugeint "
    "itself, so `k - lo` overflows INT128 once the key span passes 2^127",
)
def test_rt_a19_duckdb_hugeint_key_span_overflows(engines):
    """The int64 overflow of CLAUDE.md 4.5 again, one type wider: widening to
    hugeint is no widening for a hugeint column. A crash, not a wrong answer.
    Values: keys -(2^127 - 1) and 2^127 - 1, x differs on the second.
    """
    import duckdb

    big = 2**127 - 1
    ddl = "id hugeint, x int"
    try:
        r = run_pair(
            engines,
            "a19",
            ("duckdb", ddl, (), [f"insert into {{t}} values ({-big}, 1), ({big}, 2)"]),
            ("duckdb", ddl, (), [f"insert into {{t}} values ({-big}, 1), ({big}, 3)"]),
        )
    except ValueError as exc:
        # The engine re-raises dialect errors naming side and table (RT-D12),
        # chained to the driver's own exception. Only the stated overflow
        # counts as reproducing; anything else errors.
        if not isinstance(exc.__cause__, duckdb.OutOfRangeException):
            raise
        assert "INT128" in str(exc) and "side A" in str(exc), exc
        raise AssertionError(f"RT-A19 reproduced: {exc}") from exc
    assert found(r) == [(big, "different")]


# ---------------------------------------------------------------------
# RT-A20  MySQL TIMESTAMP is an instant, but is treated as naive
# ---------------------------------------------------------------------


def test_rt_a20_mysql_timestamp_is_not_flagged_naive(engines):
    """Cosmetic: the comparison itself is right (the session is pinned to UTC),
    but the warning points a reader at a schema problem that is not there -
    and the reverse case, MySQL TIMESTAMP against a naive timestamp, gets no
    warning. BigQuery's dialect sets `tz_aware` for exactly this reason.

    Values: the same instants, inserted on MySQL under session +05:00; row 2
    differs by one second.
    """
    ins_my = (
        "insert into {t} values (1, '2024-06-15 17:00:00'), (2, '2024-01-01 05:00:00')"
    )
    ins_pg = (
        "insert into {t} values (1, '2024-06-15 12:00:00+00'), "
        "(2, '2024-01-01 00:00:01+00')"
    )
    r = run_pair(
        engines,
        "a20",
        (
            "mysql",
            "id int, t timestamp(6)",
            (),
            [
                "set session time_zone = '+05:00'",
                ins_my,
                "set session time_zone = '+00:00'",
            ],
        ),
        ("postgres", "id int, t timestamptz", (), [ins_pg]),
    )
    assert found(r) == [(2, "different")]
    assert not any("timezone-aware" in w for w in r.warnings), r.warnings


# ---------------------------------------------------------------------
# More attacks withstood (each with a planted difference)
# ---------------------------------------------------------------------


def test_mysql_latin1_mojibake_is_not_the_utf8_it_resembles(engines):
    """A latin1 column holding 'Ã©' has the bytes C3 A9 - exactly UTF-8 'é'.
    The separator is `char(31 using utf8mb4)`, so the join converts the column
    to utf8mb4 instead of hashing its raw bytes: no collision, and a real 'é'
    in latin1 still matches PostgreSQL's."""
    r = run_pair(
        engines,
        "ok_latin1",
        (
            "mysql",
            "id int, s varchar(20) character set latin1",
            [(1, "Ã©"), (2, "é"), (3, "x")],
        ),
        ("postgres", "id int, s text", [(1, "é"), (2, "é"), (3, "y")]),
    )
    assert found(r) == [(1, "different"), (3, "different")]


@pytest.mark.parametrize("e", ["duckdb", "mysql"])
def test_trailing_nul_is_seen_off_sql_server(engines, e):
    """A trailing NUL survives DuckDB's and MySQL's fetch (contrast RT-A9)."""
    ddl = f"id int, s {TEXT[e]}"
    r = run_pair(
        engines,
        f"ok_tnul_{e}",
        (e, ddl, [(1, "a\x00"), (2, "x"), (3, "same")]),
        (e, ddl, [(1, "a"), (2, "y"), (3, "same")]),
    )
    assert found(r) == [(1, "different"), (2, "different")]


def test_mssql_uniqueidentifier_matches_postgres_uuid(engines):
    """SQL Server prints GUIDs in upper case; the dialect lower-cases them."""
    u = "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"
    v = "b1eebc99-9c0b-4ef8-bb6d-6bb9bd380a12"
    ins = "insert into {t} values (1, '%s'), (2, '%s')"
    r = run_pair(
        engines,
        "ok_uuid",
        ("mssql", "id int, u uniqueidentifier", (), [ins % (u, u)]),
        ("postgres", "id int, u uuid", (), [ins % (u, v)]),
    )
    assert found(r) == [(2, "different")]


def test_mssql_datetimeoffset_is_compared_as_an_instant(engines):
    """14:00 +02:00 on SQL Server is 12:00 UTC on PostgreSQL."""
    ins_ms = (
        "insert into {t} values (1, '2024-06-15 14:00:00 +02:00'), "
        "(2, '2024-01-01 00:00:00 +00:00')"
    )
    ins_pg = (
        "insert into {t} values (1, '2024-06-15 12:00:00+00'), "
        "(2, '2024-01-01 00:00:01+00')"
    )
    r = run_pair(
        engines,
        "ok_dto",
        ("mssql", "id int, t datetimeoffset(6)", (), [ins_ms]),
        ("postgres", "id int, t timestamptz", (), [ins_pg]),
    )
    assert found(r) == [(2, "different")]


def test_mssql_trailing_space_text_key_is_its_own_row(engines):
    """'a ' and 'a' are one value under every SQL Server collation, but two
    keys here: uniqueness is counted on a digest and rows match in Python."""
    r = run_pair(
        engines,
        "ok_ms_tkey",
        ("mssql", "k nvarchar(10), x int", [("a ", 1), ("a", 2)]),
        ("duckdb", "k varchar, x int", [("a", 2), ("b", 1)]),
        key="k",
    )
    assert sorted(found(r)) == [("a ", "only_in_a"), ("b", "only_in_b")]


def test_mysql_pad_space_collation_text_key_is_exact(engines):
    """Under PAD SPACE (utf8mb4_general_ci) 'a ' = 'a' in MySQL, but the key's
    identity goes through cast(.. as char) and Python, so both stay distinct."""
    r = run_pair(
        engines,
        "ok_my_pad",
        (
            "mysql",
            "k varchar(10) collate utf8mb4_general_ci, x int",
            [("a ", 1), ("a", 2)],
        ),
        ("duckdb", "k varchar, x int", [("a", 2), ("a ", 3)]),
        key="k",
    )
    assert found(r) == [("a ", "different")]


def test_postgres_numeric_nan_and_infinity_match_duckdb_double(engines):
    """numeric 'NaN'/'Infinity' and double nan/inf share the fixed tokens;
    a planted -Infinity vs Infinity still shows."""
    r = run_pair(
        engines,
        "ok_nan",
        (
            "postgres",
            "id int, n numeric",
            (),
            ["insert into {t} values (1, 'NaN'), (2, 'Infinity'), (3, '-Infinity')"],
        ),
        (
            "duckdb",
            "id int, n double",
            (),
            ["insert into {t} values (1, 'nan'), (2, 'inf'), (3, 'inf')"],
        ),
    )
    assert found(r) == [(3, "different")]


@pytest.mark.parametrize(
    ("a", "b", "bottom", "top"),
    [
        (
            ("postgres", "id bigint, x int"),
            ("mssql", "id bigint, x int"),
            -(2**63),
            2**63 - 1,
        ),
        (
            ("duckdb", "id ubigint, x int"),
            ("mysql", "id bigint unsigned, x int"),
            0,
            2**64 - 1,
        ),
    ],
    ids=["bigint_full_range", "ubigint_max"],
)
def test_integer_key_extremes(engines, a, b, bottom, top, request):
    """Keys at the very edges of bigint / unsigned bigint walk cleanly."""
    r = run_pair(
        engines,
        f"ok_ext_{request.node.callspec.id}",
        (*a, (), [f"insert into {{t}} values ({bottom}, 1), ({top}, 2)"]),
        (*b, (), [f"insert into {{t}} values ({bottom}, 1), ({top}, 3)"]),
    )
    assert found(r) == [(top, "different")]
