"""FLOAT canonical text: one definition, every engine, byte for byte.

Two doubles compare equal iff their canonical text is byte-identical, so the
FLOAT expression is part of the correctness contract. Before this was pinned
down, PostgreSQL rendered a double through 15 significant digits and DuckDB
through `x * 10^6` in double precision, and on the adversarial sample in
`float_sample.py` they disagreed on 347 of 1,565 values. Worse than the false
alarms, it could hide a real difference: PostgreSQL rendered 1e15+0.5 as
`1000000000000000.000000`, exactly DuckDB's text for 1e15, so a changed value
compared equal. Both are tested below.

The definition is `canonical_float_text` in `parity.dialects.base`: below 2^53
the shortest round-trip decimal rounded half away from zero to the float scale;
from 2^53 the exact integer; at 2^116 and beyond a loud refusal. Every live
engine is checked against that Python spec directly, which makes every pair
agree by transitivity - and the PostgreSQL <-> DuckDB and PostgreSQL <-> MySQL
pairs are also diffed end to end, with planted differences.
"""

from __future__ import annotations

import math

import pytest
from conftest import PG_SCHEMA, duckdb_write, open_duckdb, open_mysql, open_pg
from float_sample import adversarial_doubles, render

from parity.dialects.base import (
    FLOAT_MAX_MAGNITUDE,
    NULL_SENTINEL,
    canonical_float_text,
    get_dialect,
)
from parity.engine import diff

SAMPLE = adversarial_doubles()

# ------------------------------------------------------------ the spec itself


@pytest.mark.parametrize(
    ("x", "scale", "text"),
    [
        (1.5, 6, "1.500000"),
        (-0.125, 6, "-0.125000"),
        (1 / 3, 6, "0.333333"),
        # Exact ties at the sixth place round half away from zero.
        (0.0078125, 6, "0.007813"),
        (-0.0078125, 6, "-0.007813"),
        # The shortest repr is the value the user typed, so these round up even
        # though the binary value sits a hair below the halfway point.
        (2.0000005, 6, "2.000001"),
        (123456789.1234565, 6, "123456789.123457"),
        (-262.4724295, 6, "-262.472430"),
        # 16-17 significant digits survive; float8::numeric dropped them.
        (1e15 + 0.5, 6, "1000000000000000.500000"),
        # 2^40 + 1/128 is exactly ...776.0078125, but its shortest repr is
        # ...776.0078 - digits past what the double distinguishes are zeros.
        (2**40 + 1 / 128, 6, "1099511627776.007800"),
        # From 2^53 the exact integer, not the shortest repr (3.880963501053742e16).
        (38809635010537424.0, 6, "38809635010537424.000000"),
        (1e20 / 3, 6, "33333333333333331968.000000"),
        (float(2**53 + 2), 0, "9007199254740994"),
        # Signed zeros, and anything that rounds to zero, have no sign.
        (-0.0, 6, "0.000000"),
        (-1e-7, 6, "0.000000"),
        (5e-324, 6, "0.000000"),
        (4.9999999999e-7, 6, "0.000000"),
        (5e-7, 6, "0.000001"),
        # The scale is honoured, including scale 0.
        (0.5, 0, "1"),
        (-2.5, 0, "-3"),
        (0.0078125, 2, "0.01"),
        (-0.001, 2, "0.00"),
    ],
)
def test_the_spec_on_known_values(x: float, scale: int, text: str):
    """The Python spec, on values whose right answer is known by hand."""
    assert canonical_float_text(x, scale) == text


def test_the_spec_refuses_what_no_engine_can_render():
    """Exact just below 2^116; refused at it, past it and for non-finite."""
    assert canonical_float_text(2.0**116 - 2.0**63) == (
        str(int(2.0**116 - 2.0**63)) + ".000000"
    )
    for x in (float(FLOAT_MAX_MAGNITUDE), -1e40, math.inf, math.nan):
        with pytest.raises(ValueError):
            canonical_float_text(x)


def test_equal_doubles_render_equal_and_the_sample_is_hard():
    """The spec is a function of the IEEE value; and the sample exercises it.

    A sample on which a naive rendering already agrees would prove nothing:
    assert that rounding the exact binary value instead (the other defensible
    definition) disagrees with the spec on a good share of it.
    """
    from decimal import ROUND_HALF_UP, Context, Decimal

    assert canonical_float_text(0.1 + 0.2) == canonical_float_text(0.30000000000000004)
    ctx = Context(prec=1200)
    q = Decimal("0.000001")

    def exact(x: float) -> str:
        """The exact binary value, rounded half up - the rejected definition."""
        d = Decimal(x).quantize(q, rounding=ROUND_HALF_UP, context=ctx)
        return "0.000000" if d == 0 else format(d, "f")

    differs = sum(exact(x) != canonical_float_text(x) for x in SAMPLE)
    assert differs > 200, differs  # 215 of 1,565


# ------------------------------------------------- every live engine vs spec

ENGINES = [
    pytest.param("postgres", marks=pytest.mark.postgres),
    pytest.param("duckdb", marks=pytest.mark.duckdb),
    pytest.param("mysql", marks=pytest.mark.mysql),
    pytest.param("bigquery", marks=pytest.mark.bigquery),
]


@pytest.fixture(params=ENGINES)
def engine(request) -> str:
    """Each engine in turn; unreachable ones skip through their URL fixture."""
    return str(request.param)


def _url(request, engine: str) -> str:
    """The engine's URL from conftest, which skips when it is unreachable."""
    if engine == "duckdb":
        pytest.importorskip("duckdb")
        return "duckdb:///:memory:"
    fixture = {"postgres": "pg_url", "mysql": "mysql_url", "bigquery": "bigquery_url"}
    return str(request.getfixturevalue(fixture[engine]))


@pytest.mark.parametrize("scale", [6, 2])
def test_every_engine_renders_the_adversarial_sample_exactly_as_the_spec(
    request, engine: str, scale: int
):
    """1,565 hard doubles, at the default scale and at --float-scale 2."""
    d = get_dialect(_url(request, engine), side="A", float_scale=scale)
    try:
        got = render(d, engine, SAMPLE)
    finally:
        d.close()
    bad = [
        (x, g, canonical_float_text(x, scale))
        for x, g in zip(SAMPLE, got, strict=True)
        if g != canonical_float_text(x, scale)
    ]
    assert not bad, f"{engine} disagrees with the spec on {len(bad)}: {bad[:5]}"


def test_every_engine_refuses_past_2_to_the_116_and_renders_just_below(
    request, engine: str
):
    """Past 2^116 the split into two 64-bit halves overflows. Every engine must
    raise - MySQL's CAST would otherwise clamp silently, making two different
    huge values equal - and the largest double below must still be exact."""
    d = get_dialect(_url(request, engine), side="A")
    try:
        below = 2.0**116 - 2.0**63
        assert render(d, engine, [below, -below]) == [
            canonical_float_text(below),
            canonical_float_text(-below),
        ]
        for x in (2.0**116, -1e300):
            with pytest.raises(Exception, match=r"out of (the comparable )?range"):
                render(d, engine, [x])
            if engine == "postgres":
                d._conn.rollback()  # the failed statement aborted the snapshot
    finally:
        d.close()


# ------------------------------------------------------ end to end, planted

#: (id, side A, side B). Rows 1-3 differ at the sixth decimal and must be
#: found; the first two are the ones the old PostgreSQL rendering *hid*,
#: rendering A's value as exactly B's text. Rows 4-5 differ only past the
#: scale (in the 7th decimal) and must compare equal, as documented. Every
#: other row is the sample, identical on both sides.
PLANTED = [
    (1, 1e15 + 0.5, 1e15),
    (2, 123456789.1234565, 123456789.123456),
    (3, -262.4724295, -262.472429),
    (4, 1.0000001, 1.0000002),
    (5, 1e9 + 1e-7, 1e9 + 2e-7),
]
FOUND = [1, 2, 3]
BASE = 100  # sample rows start here


def _rows(side: int) -> list[tuple[int, float]]:
    """Side 1 (A) or 2 (B): the planted rows, then the shared sample."""
    planted = [(i, (a, b)[side - 1]) for i, a, b in PLANTED]
    return planted + [(BASE + n, x) for n, x in enumerate(SAMPLE)]


SCHEMA = f"{PG_SCHEMA}_float"


@pytest.fixture(scope="module")
def pg_side(pg_url):
    """Side A on PostgreSQL: `fa` (side-A values) and `fb` (side-B values)."""
    import psycopg

    con = psycopg.connect(pg_url, autocommit=True)
    try:
        con.execute(f"drop schema if exists {SCHEMA} cascade")
        con.execute(f"create schema {SCHEMA}")
        for name, side in (("fa", 1), ("fb", 2)):
            con.execute(f"create table {SCHEMA}.{name} (id bigint primary key, x float8)")
            with con.cursor() as cur:
                cur.executemany(
                    f"insert into {SCHEMA}.{name} values (%s, %s)", _rows(side)
                )
    finally:
        con.close()
    d = open_pg(pg_url, side="A")
    yield d
    d.close()


@pytest.fixture(scope="module")
def duck_float_path(duckdb_path):
    """A DuckDB file holding `fa` and `fb`, written once and then closed."""
    path = duckdb_path.replace(".duckdb", "_float.duckdb")
    con = duckdb_write(path)
    for name, side in (("fa", 1), ("fb", 2)):
        con.execute(f"create table {name} (id bigint, x double)")
        con.executemany(f"insert into {name} values (?, ?)", _rows(side))
    con.close()
    return path


@pytest.fixture(scope="module")
def duck_side(duck_float_path):
    """Side B on DuckDB, read-only."""
    d = open_duckdb(duck_float_path, side="B")
    yield d
    d.close()


@pytest.fixture(scope="module")
def mysql_side(mysql_url):
    """Side B on MySQL: `float_canon_a` and `float_canon_b`."""
    d = open_mysql(mysql_url, side="B")
    cur = d._conn.cursor()
    try:
        for name, side in (("float_canon_a", 1), ("float_canon_b", 2)):
            cur.execute(f"drop table if exists {name}")
            cur.execute(f"create table {name} (id bigint primary key, x double)")
            cur.executemany(f"insert into {name} values (%s, %s)", _rows(side))
        d._conn.commit()
    finally:
        cur.close()
    yield d
    d.close()


def _assert_loaded(d, table: str, side: int) -> None:
    """The values arrived bit-exact, so any finding is the rendering's."""
    got = dict(d.query(f"select id, x from {table}"))
    want = dict(_rows(side))
    assert {k: got[k] for k in want} == want


def test_postgres_and_duckdb_read_the_sample_identical(pg_side, duck_side):
    """347 rows of rounding-only false alarms before; none now."""
    _assert_loaded(pg_side, f"{SCHEMA}.fa", 1)
    _assert_loaded(duck_side, "fa", 1)
    result = diff(pg_side, duck_side, f"{SCHEMA}.fa", "fa", "id")
    assert result.identical, [(r.key, r.values_a, r.values_b) for r in result.diffs[:5]]
    assert result.stats.rows_downloaded == 0


def test_postgres_and_duckdb_find_exactly_the_planted_float_changes(pg_side, duck_side):
    """Sixth-decimal changes are found - including the two the old rendering
    hid - and changes past the float scale are equal, as documented."""
    _assert_loaded(duck_side, "fb", 2)
    result = diff(pg_side, duck_side, f"{SCHEMA}.fa", "fb", "id")
    assert sorted((r.key, r.kind) for r in result.diffs) == [
        (k, "different") for k in FOUND
    ]
    assert all(r.columns == ["x"] for r in result.diffs)
    # And the same both ways round.
    result = diff(duck_side, pg_side, "fa", f"{SCHEMA}.fb", "id")
    assert sorted(r.key for r in result.diffs) == FOUND


def test_postgres_and_mysql_find_exactly_the_planted_float_changes(pg_side, mysql_side):
    """The same planted rows between PostgreSQL and MySQL, neither the reference."""
    _assert_loaded(mysql_side, "float_canon_a", 1)
    result = diff(pg_side, mysql_side, f"{SCHEMA}.fa", "float_canon_a", "id")
    assert result.identical, [(r.key, r.values_a, r.values_b) for r in result.diffs[:5]]
    result = diff(pg_side, mysql_side, f"{SCHEMA}.fa", "float_canon_b", "id")
    assert sorted((r.key, r.kind) for r in result.diffs) == [
        (k, "different") for k in FOUND
    ]


def test_a_change_past_the_scale_becomes_visible_at_a_finer_scale(
    pg_side, pg_url, duck_float_path
):
    """--float-scale is the documented knob: rows 4 and 5 differ in the 7th
    decimal, so they are equal at 6 places and different at 7."""
    pg7 = open_pg(pg_url, side="A", float_scale=7)
    duck7 = open_duckdb(duck_float_path, side="B", float_scale=7)
    try:
        result = diff(pg7, duck7, f"{SCHEMA}.fa", "fb", "id")
    finally:
        pg7.close()
        duck7.close()
    assert sorted(r.key for r in result.diffs) == [*FOUND, 4, 5]


# ------------------------------------------------------- unverified engines


def test_an_unverified_float_rendering_is_warned_about_in_the_output():
    """Snowflake's FLOAT path predates the canonical form and could not be
    checked live, so a FLOAT column compared on it carries a warning - in the
    result, not only the docs (CLAUDE.md section 8). Other columns do not."""
    from fakes import FakeDialect, SyntheticTable

    from parity.dialects.snowflake_dialect import SnowflakeDialect
    from parity.types import Column, LogicalType

    assert SnowflakeDialect().float_caveat()
    from parity.dialects.bigquery_dialect import BigQueryDialect
    from parity.dialects.duckdb_dialect import DuckDBDialect
    from parity.dialects.mysql_dialect import MySQLDialect
    from parity.dialects.postgres_dialect import PostgresDialect

    for cls in (PostgresDialect, DuckDBDialect, MySQLDialect, BigQueryDialect):
        assert cls().float_caveat() is None, cls.__name__

    class Unverified(FakeDialect):
        """A fake whose FLOAT rendering is declared unverified."""

        def float_caveat(self) -> str | None:
            """The caveat under test."""
            return "unverified float path"

    # SyntheticTable generates two values per row.
    cols = [
        Column("ratio", LogicalType.FLOAT, "double"),
        Column("status", LogicalType.STRING, "varchar"),
    ]
    a = Unverified(SyntheticTable(50, columns=cols), side="A")
    b = FakeDialect(SyntheticTable(50, columns=cols), side="B")
    warned = [w for w in diff(a, b, "t", "t", "id").warnings if "unverified" in w]
    assert warned == ["side A (fake) FLOAT columns ['ratio']: unverified float path"]

    a = Unverified(SyntheticTable(50), side="A")  # decimal + string only
    b = FakeDialect(SyntheticTable(50), side="B")
    assert not [w for w in diff(a, b, "t", "t", "id").warnings if "unverified" in w]


def test_a_float_scale_past_the_maximum_is_refused_for_float_columns():
    """Past FLOAT_MAX_SCALE MySQL's DECIMAL cast clamps silently (2e15 at 23
    places became 999999999999999.999...) and BigQuery's 38 places run out,
    so a FLOAT column is refused rather than compared approximately. The
    maximum itself is verified live by `demo/float_agreement.py --scale 20`."""
    from parity.dialects.base import FLOAT_MAX_SCALE
    from parity.dialects.bigquery_dialect import BigQueryDialect
    from parity.dialects.duckdb_dialect import DuckDBDialect
    from parity.dialects.mysql_dialect import MySQLDialect
    from parity.dialects.postgres_dialect import PostgresDialect
    from parity.types import Column, LogicalType

    col = Column("ratio", LogicalType.FLOAT, "double")
    for cls in (PostgresDialect, DuckDBDialect, MySQLDialect, BigQueryDialect):
        cls(float_scale=FLOAT_MAX_SCALE).normalize(col)  # no raise
        with pytest.raises(ValueError, match="cannot be compared at float_scale 21"):
            cls(float_scale=FLOAT_MAX_SCALE + 1, side="B").normalize(col)


def test_a_null_double_is_the_sentinel_on_every_engine(request, engine: str):
    """A NULL fails every magnitude test in the FLOAT expression, so it must not
    reach the out-of-range branch: BigQuery's ERROR(NULL) raised on it when that
    branch was an `else`. NULL must come out as the NULL sentinel, like any
    other type, and differ from every real value's text."""
    from float_sample import DOUBLE_TYPE, FLOAT_COLUMN

    d = get_dialect(_url(request, engine), side="A")
    try:
        expr = d.normalize(FLOAT_COLUMN)
        rows = d.query(
            f"select {expr} from (select cast(null as {DOUBLE_TYPE[engine]}) as x) t"
        )
    finally:
        d.close()
    assert [tuple(r) for r in rows] == [(NULL_SENTINEL,)]
