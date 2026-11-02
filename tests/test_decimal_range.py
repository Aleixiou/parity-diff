"""DECIMAL values too wide for the rendering cast: exact, or a loud failure.

MySQL's `CAST(x AS DECIMAL(p,s))` does not raise when `x` does not fit - inside
a SELECT, in every sql_mode, it clamps to the type's maximum and leaves only a
warning. The DECIMAL rendering used to be `decimal(38, float_scale)`, so every
DECIMAL(65,0) value from 1e32 up rendered as
`99999999999999999999999999999999.999999`: 1e32 and 5e40 compared equal to each
other, and to a PostgreSQL or DuckDB row really holding that maximum. That was
reproduced as a false match end to end against all three, before the fix.

Every test here plants values that the old cast clamped to one text and asserts
the tool now tells them apart - or refuses, naming side, column and value - and
that in-range values still render exactly what they always did, so no checksum
moved. The other engines are checked for the same class of bug: DuckDB and
BigQuery raise on overflow and PostgreSQL's numeric is exact.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Context, Decimal

import pytest
from conftest import PG_SCHEMA, duckdb_write, open_duckdb, open_mysql, open_pg

from parity.dialects.mysql_dialect import MySQLDialect
from parity.engine import diff
from parity.types import Column, LogicalType

SCHEMA = f"{PG_SCHEMA}_decrange"

#: The value MySQL's old `decimal(38,6)` cast clamped everything >= 1e32 to.
CLAMP_6 = "99999999999999999999999999999999.999999"
#: And `decimal(38,20)`, which clamped from 1e18.
CLAMP_20 = "999999999999999999.99999999999999999999"


def _expected(value: str, scale: int) -> str:
    """The canonical text: round half away from zero to `scale` places."""
    ctx = Context(prec=200)
    q = Decimal(value).quantize(
        Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP, context=ctx
    )
    return format(q, "f") if q != 0 else format(abs(q), "f")


def _mysql_table(mysql_url: str, name: str, ddl_type: str, rows: dict[int, str]) -> None:
    """(Re)create a MySQL table `name(id, v ddl_type)` holding `rows`, committed."""
    d = open_mysql(mysql_url, side="A")
    cur = d._conn.cursor()
    try:
        cur.execute(f"drop table if exists {name}")
        cur.execute(f"create table {name} (id int primary key, v {ddl_type})")
        for k, v in rows.items():
            cur.execute(f"insert into {name} values ({k}, {v})")
        d._conn.commit()
    finally:
        cur.close()
        d.close()


def _pg_table(pg_url: str, name: str, rows: dict[int, str]) -> str:
    """(Re)create an unconstrained-numeric PostgreSQL table holding `rows`."""
    import psycopg

    con = psycopg.connect(pg_url, autocommit=True)
    try:
        con.execute(f"create schema if not exists {SCHEMA}")
        con.execute(f"drop table if exists {SCHEMA}.{name}")
        con.execute(f"create table {SCHEMA}.{name} (id int primary key, v numeric)")
        for k, v in rows.items():
            con.execute(f"insert into {SCHEMA}.{name} values ({k}, {v})")
    finally:
        con.close()
    return f"{SCHEMA}.{name}"


def _duck_table(path: str, ddl_type: str, rows: dict[int, str]) -> str:
    """Write a DuckDB file holding `main.t(id, v ddl_type)`."""
    con = duckdb_write(path)
    try:
        con.execute(f"create table t (id integer primary key, v {ddl_type})")
        for k, v in rows.items():
            con.execute(f"insert into t values ({k}, {v})")
    finally:
        con.close()
    return "main.t"


def _run(a, b, a_table: str, b_table: str):
    """Diff on `id`, closing both dialects whatever happens."""
    try:
        return diff(a, b, a_table, b_table, key="id")
    finally:
        a.close()
        b.close()


def _found(result) -> dict[int | str, tuple[str, list[str]]]:
    """Each reported difference as key -> (kind, columns)."""
    return {d.key: (d.kind, d.columns) for d in result.diffs}


# ------------------------------------------------------------- offline


def test_a_scale_mysql_cannot_cast_to_is_refused_naming_side_and_column():
    """MySQL caps DECIMAL scale at 30 (error 1425); refuse before any query."""
    d = MySQLDialect(float_scale=31, side="B")
    with pytest.raises(ValueError, match=r"side B.*'amount'.*float_scale 31.*30"):
        d.normalize(Column("amount", LogicalType.DECIMAL, "decimal"))


@pytest.mark.parametrize("scale", [0, 6, 20, 30])
def test_the_guard_is_the_cast_types_own_maximum(scale):
    """The bound is 10^(65-s) - 10^-s, the largest DECIMAL(65,s): exact, not
    approximate, because no MySQL DECIMAL holds the 66 digits it would take for
    a value on either side of it to round across it."""
    sql = MySQLDialect(float_scale=scale).normalize(
        Column("v", LogicalType.DECIMAL, "decimal")
    )
    widest = "9" * (65 - scale) + ("." + "9" * scale if scale else "")
    assert f"abs(`v`) > {widest} " in sql
    assert f"decimal(65,{scale})" in sql
    assert "uuid_to_bin" in sql


# ---------------------------------------------------- MySQL rendering


@pytest.mark.mysql
@pytest.mark.parametrize("scale", [0, 6, 20, 30])
def test_in_range_values_render_exactly_as_the_old_cast_did(mysql_url, scale):
    """No checksum moves: every value the old decimal(38,s) cast could hold
    renders byte-identically through the new decimal(65,s) path."""
    values = [
        "0",
        "1.5",
        "-1.5",
        "2.0000005",
        "-2.0000005",
        "0.0000001",
        "-0.0000001",
        "0.5",
        "-0.5",
        "123456789.123456789",
        "-99999.99",
        # The widest value the old cast held at this scale, rounding down.
        "9" * (38 - scale) + ".4",
        "-" + "9" * (38 - scale) + ".4",
    ]
    d = open_mysql(mysql_url, float_scale=scale)
    try:
        col = Column("v", LogicalType.DECIMAL, "decimal")
        for v in values:
            if len(v.lstrip("-").split(".")[0]) > 38 - scale:
                continue  # the old cast clamped this one: nothing to preserve
            src = f"(select cast({v} as decimal(65,10)) as `v`) s"
            new = d.query(f"select {d.normalize(col)} from {src}")[0][0]
            old = d.query(
                f"select cast(cast(`v` as decimal(38,{scale})) as char) from {src}"
            )
            assert new == old[0][0] == _expected(v, scale), v
    finally:
        d.close()


@pytest.mark.mysql
@pytest.mark.parametrize("value", ["1" + "0" * 32, "-5" + "0" * 40, "9" * 59])
def test_values_past_the_old_clamp_now_render_exactly(mysql_url, value):
    """From 1e32 to 59 integer digits the widened cast renders exactly what
    PostgreSQL does, instead of the clamped maximum."""
    d = open_mysql(mysql_url)
    try:
        col = Column("v", LogicalType.DECIMAL, "decimal")
        src = f"(select cast({value} as decimal(65,0)) as `v`) s"
        assert d.query(f"select {d.normalize(col)} from {src}")[0][0] == _expected(
            value, 6
        )
    finally:
        d.close()


# ----------------------------------------------- end to end, planted


@pytest.mark.postgres
@pytest.mark.mysql
@pytest.mark.parametrize(
    ("scale", "ddl", "big", "neg", "clamp"),
    [
        (6, "decimal(65,0)", "1" + "0" * 32, "-5" + "0" * 40, CLAMP_6),
        (20, "decimal(30,0)", "1" + "0" * 18, "-5" + "0" * 25, CLAMP_20),
    ],
)
def test_mysql_values_the_old_cast_clamped_differ_from_postgres(
    mysql_url, pg_url, scale, ddl, big, neg, clamp
):
    """The reproduced false match. PostgreSQL holds the clamp value itself, so
    under the old cast rows 1 and 2 read identical. Row 3 is equal and must stay
    equal; row 4 is an ordinary planted change."""
    _mysql_table(mysql_url, "dec_range", ddl, {1: big, 2: neg, 3: "15", 4: "7"})
    pg_t = _pg_table(pg_url, "dec_range", {1: clamp, 2: "-" + clamp, 3: "15", 4: "8"})
    result = _run(
        open_mysql(mysql_url, side="A", float_scale=scale),
        open_pg(pg_url, side="B", float_scale=scale),
        "dec_range",
        pg_t,
    )
    assert not result.identical
    assert _found(result) == {k: ("different", ["v"]) for k in (1, 2, 4)}
    by_key = {d.key: d for d in result.diffs}
    assert by_key[1].values_a["v"] == _expected(big, scale)
    assert by_key[1].values_b["v"] == clamp


@pytest.mark.duckdb
@pytest.mark.mysql
@pytest.mark.parametrize(
    ("scale", "ddl", "big", "neg", "clamp"),
    [
        (6, "decimal(65,0)", "1" + "0" * 32, "-5" + "0" * 40, CLAMP_6),
        (20, "decimal(30,0)", "1" + "0" * 18, "-5" + "0" * 25, CLAMP_20),
    ],
)
def test_mysql_values_the_old_cast_clamped_differ_from_duckdb(
    mysql_url, tmp_path, scale, ddl, big, neg, clamp
):
    """The same false match against DuckDB, whose decimal(38,s) holds the clamp
    value exactly."""
    _mysql_table(mysql_url, "dec_range", ddl, {1: big, 2: neg, 3: "15", 4: "7"})
    path = str(tmp_path / "d.duckdb")
    duck_t = _duck_table(
        path, f"decimal(38,{scale})", {1: clamp, 2: "-" + clamp, 3: "15", 4: "8"}
    )
    result = _run(
        open_mysql(mysql_url, side="A", float_scale=scale),
        open_duckdb(path, side="B", float_scale=scale),
        "dec_range",
        duck_t,
    )
    assert not result.identical
    assert _found(result) == {k: ("different", ["v"]) for k in (1, 2, 4)}


@pytest.mark.mysql
def test_two_different_huge_mysql_values_are_told_apart(mysql_url):
    """MySQL against itself: 1e32 and 2e32 both clamped to one text."""
    _mysql_table(mysql_url, "dec_range", "decimal(65,0)", {1: "1" + "0" * 32, 2: "3"})
    _mysql_table(mysql_url, "dec_range_b", "decimal(65,0)", {1: "2" + "0" * 32, 2: "3"})
    result = _run(
        open_mysql(mysql_url, side="A"),
        open_mysql(mysql_url, side="B"),
        "dec_range",
        "dec_range_b",
    )
    assert _found(result) == {1: ("different", ["v"])}


# ---------------------------------------------------- loud failure


@pytest.mark.postgres
@pytest.mark.mysql
@pytest.mark.parametrize(
    ("scale", "value"),
    [(6, "1" + "0" * 59), (6, "-2" + "0" * 59), (30, "1" + "0" * 35)],
)
def test_a_value_too_wide_even_for_decimal_65_fails_loudly(
    mysql_url, pg_url, scale, value
):
    """Past 65 - s integer digits MySQL has no type that renders the value, so
    the diff must refuse - naming side, column and the exact value - rather than
    compare a clamped one. PostgreSQL holds a *different* value here, which
    the clamp would have hidden."""
    _mysql_table(mysql_url, "dec_range", "decimal(65,0)", {1: value, 2: "3"})
    pg_t = _pg_table(pg_url, "dec_range", {1: value + "1", 2: "3"})
    with pytest.raises(ValueError) as info:
        _run(
            open_mysql(mysql_url, side="A", float_scale=scale),
            open_pg(pg_url, side="B", float_scale=scale),
            "dec_range",
            pg_t,
        )
    message = str(info.value)
    assert message.startswith(
        f"[side A: mysql] parity: DECIMAL value {value} in column 'v' does not fit "
        f"DECIMAL(65,{scale})"
    ), message
    assert f"float_scale {scale}" in message
    assert "clamp" in message


@pytest.mark.duckdb
@pytest.mark.mysql
def test_a_value_too_wide_fails_loudly_against_duckdb(mysql_url, tmp_path):
    """The same refusal against DuckDB: 7e60 cannot be rendered at scale 6."""
    _mysql_table(mysql_url, "dec_range", "decimal(65,0)", {1: "7" + "0" * 60, 2: "3"})
    path = str(tmp_path / "d.duckdb")
    duck_t = _duck_table(path, "decimal(38,6)", {1: "1", 2: "3"})
    value = "7" + "0" * 60
    with pytest.raises(ValueError, match=rf"side A: mysql\] .* {value} in column 'v'"):
        _run(open_mysql(mysql_url, side="A"), open_duckdb(path), "dec_range", duck_t)


# --------------------------------------- the other engines, same class


@pytest.mark.duckdb
@pytest.mark.parametrize(
    ("scale", "value"), [(6, "1" + "0" * 32), (6, "-1" + "0" * 32), (20, "1" + "0" * 18)]
)
def test_duckdb_raises_rather_than_clamps(tmp_path, scale, value):
    """DuckDB's decimal(38,s) cast raises "value is out of range" - loud."""
    path = str(tmp_path / "d.duckdb")
    _duck_table(path, "decimal(38,0)", {1: value})
    d = open_duckdb(path, float_scale=scale)
    try:
        col = Column("v", LogicalType.DECIMAL, "DECIMAL(38,0)")
        with pytest.raises(Exception, match="out of range"):
            d.query(f"select {d.normalize(col)} from main.t")
    finally:
        d.close()


@pytest.mark.postgres
@pytest.mark.parametrize("scale", [6, 20, 30])
def test_postgres_numeric_renders_huge_values_exactly(pg_url, scale):
    """PostgreSQL's arbitrary-precision numeric has no range to clamp to."""
    d = open_pg(pg_url, float_scale=scale)
    try:
        col = Column("v", LogicalType.DECIMAL, "numeric")
        for value in ("1" + "0" * 80, "-" + "9" * 65 + ".9999995", "1" + "0" * 32):
            got = d.query(
                f"select {d.normalize(col)} from (select {value}::numeric as v) s"
            )
            assert got[0][0] == _expected(value, scale)
    finally:
        d.close()


@pytest.mark.bigquery
def test_bigquery_numeric_raises_rather_than_clamps(bigquery_url):
    """BigQuery's ROUND overflows loudly: NUMERIC's maximum rounded to 6 places
    carries past what NUMERIC holds, and the query fails naming the value."""
    from conftest import open_bigquery

    d = open_bigquery(bigquery_url)
    try:
        col = Column("v", LogicalType.DECIMAL, "NUMERIC")
        for lit in (
            "99999999999999999999999999999.999999999",
            "-99999999999999999999999999999.999999999",
        ):
            with pytest.raises(Exception, match="overflow"):
                d.query(f"select {d.normalize(col)} from (select numeric '{lit}' as v)")
        big = "12345678901234567890123456789.123456789"
        got = d.query(f"select {d.normalize(col)} from (select numeric '{big}' as v)")
        assert got[0][0] == _expected(big, 6)
    finally:
        d.close()
