"""Round H, cross-engine: differential fuzzing of every engine pair.

`fuzz_cross.py` generates the cases (1-40 columns of every kind the rules
cover, exact keys of every kind, `--where`, `--map`, random knobs, planted
mutations) and checks each against the brute-force oracle of `fuzz_harness`
*and* the independent sample check. This module runs a few cases per pair
(`PARITY_RTH_CASES`, default 6), same-engine pairs included, and pins the
round-H findings (RT-HX<n>) as strict xfails.

A deep run, with shrunk repros printed:

    python tests/redteam/fuzz_cross.py --minutes 30 --pairs mysql-mssql

Tables are `rth_x_*`, dropped after each case. Every pair skips without its
engines; Snowflake needs the connector-4.x venv (see tests/test_snowflake.py).
"""

from __future__ import annotations

import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import float_fresh
import fuzz_cross as fx
import fuzz_harness as fh
from conftest import (
    _bigquery_available,
    _mssql_available,
    _mysql_available,
    _pg_available,
    _snowflake_available,
)
from test_fuzz import _duck, _found, _run

from parity.dialects.base import canonical_float_text

CASES = int(os.environ.get("PARITY_RTH_CASES", "6"))
SEED = int(os.environ.get("PARITY_RTH_SEED", "1"))


def _available(name: str) -> tuple[bool, str]:
    """Can this engine be used here, and if not, why not."""
    if name == "duckdb":
        try:
            import duckdb  # noqa: F401
        except ImportError:  # pragma: no cover
            return False, "duckdb is not installed"
        return True, ""
    return {
        "postgres": _pg_available,
        "mysql": _mysql_available,
        "mssql": _mssql_available,
        "bigquery": _bigquery_available,
        "snowflake": _snowflake_available,
    }[name]()


PAIRS = [*fx.all_pairs(list(fx.LOCAL)), ("bigquery", "duckdb"), ("snowflake", "duckdb")]


@pytest.mark.parametrize(("a", "b"), PAIRS, ids=lambda x: x)
def test_round_h_fuzz_agrees_with_the_oracle_and_the_sample_check(a, b):
    """Every case: exactly the oracle's differences, `identical` iff none,
    and no failure from the independent sample check."""
    for name in {a, b}:
        ok, why = _available(name)
        if not ok:
            pytest.skip(f"{name}: {why}")
    u = fx.urls()
    ea, eb = fx.open_engine(a, u), fx.open_engine(b, u)
    cases = CASES if "bigquery" not in (a, b) else min(CASES, 2)
    failures, planted = [], 0
    try:
        for i in range(cases):
            seed = SEED * 1_000_000 + PAIRS.index((a, b)) * 10_000 + i
            case = fx.random_case_h(seed, a, b)
            out = fx.run_case_h(case, ea, eb)
            planted += bool(out.expected)
            if not out.ok:
                failures.append(fx.report(out))
    finally:
        ea.close()
        eb.close()
    assert not failures, "\n".join(failures)[:6000]
    if cases >= 6:
        assert planted, "no case planted a difference"


@pytest.mark.sample_check_may_disagree
def test_the_round_h_harness_catches_a_planted_false_match(monkeypatch):
    """The oracle stays independent of the tool through `--where` and `--map`:
    with DuckDB's text escaping switched off (the old RT-E1 bug), cases that
    plant NULL against backslash-N must be caught."""
    from parity.dialects.duckdb_dialect import DuckDBDialect

    monkeypatch.setattr(DuckDBDialect, "escape_text", lambda self, expr: expr)
    verdicts = []
    for i in range(400):
        case = fh.random_case(910_000 + i, "duckdb", "duckdb", {"sentinel_text"})
        if not any(":sentinel@" in m for m in case.mutations):
            continue
        h = fx.CaseH(**{k: getattr(case, k) for k in case.__dataclass_fields__})
        rng = random.Random(i)
        fx.plan_map(rng, h)
        out = fx.run_case_h(h, fh.Duck(), fh.Duck())
        verdicts.append(out.verdict())
        if len(verdicts) >= 4:
            break
    assert verdicts, "no case planted the trigger"
    assert {"FALSE MATCH", "MISSED DIFF", "SAMPLE FAIL"} & set(verdicts), verdicts


@pytest.mark.sample_check_may_disagree
@pytest.mark.parametrize(
    ("where_a", "where_b"),
    [
        ("random() < 0.5", "random() < 0.5"),
        ("random() < 0.999", None),
        ("id % 2 = 0 or random() < 0.01", "id % 2 = 0"),
    ],
)
def test_a_filter_that_changes_between_queries_is_never_identical(
    tmp_path, where_a, where_b
):
    """A `--where` whose rows change from one query to the next (here a
    random() predicate) selects a different row set at every level of the
    walk. Identical tables (planted: the filter itself differs per query)
    must never come back identical - measured 59 of 60 runs unexplained and
    sample-failed, every one reported with differences."""
    rows = ", ".join(f"({i}, {i * 2})" for i in range(3000))
    for _ in range(3):
        a = _duck(tmp_path, f"a{_}", "id int, v int", rows, side="A")
        b = _duck(tmp_path, f"b{_}", "id int, v int", rows)
        try:
            r = fx.diff(
                a,
                b,
                "t",
                "t",
                key="id",
                where_a=where_a,
                where_b=where_b,
                threshold=50,
                bisection_factor=4,
            )
        finally:
            a.close()
            b.close()
        assert not r.identical
        assert r.diffs or r.unexplained


def test_fresh_random_doubles_render_the_spec_on_duckdb(tmp_path):
    """The FLOAT canonical form on 3,000 fresh doubles (the deep run takes
    100k per engine, `float_fresh.py`), with a planted difference: the
    neighbouring double of a tie must render differently at its scale."""
    import duckdb

    from parity.dialects.base import get_dialect
    from parity.types import Column, LogicalType

    xs = float_fresh.fresh_doubles(random.Random(5), 3000, subnormal=True)
    path = str(tmp_path / "f.duckdb")
    con = duckdb.connect(path)
    con.execute("create table t (i int, x double)")
    con.executemany("insert into t values (?, ?)", list(enumerate(xs)))
    con.close()
    d = get_dialect("duckdb:///" + path.replace("\\", "/"), side="A")
    try:
        expr = d.normalize(Column("x", LogicalType.FLOAT, "double"))
        got = dict(d.query(f"select i, {expr} from t"))
    finally:
        d.close()
    assert [i for i, x in enumerate(xs) if got[i] != canonical_float_text(x, 6)] == []
    # Planted: 0.0000005 rounds up, the double just below it does not.
    assert canonical_float_text(5e-7, 6) != canonical_float_text(4.999999999e-7, 6)


# ------------------------------------------------------------------ findings
#
# Minimal repros the round-H fuzzer shrank to (RT-HX<n>). Each asserts the
# *correct* behaviour: a strict xfail while open, an XPASS the day it is fixed.


def test_rt_hx1_duckdb_decimal_key_without_integer_digits_matches(tmp_path):
    """Fuzz seed 12000035 (duckdb->duckdb, key decimal(6,6)) reported key
    `-.517439` where the rules say `-0.517439` (`decimal_key_text`: `-0.25`).
    The same keys in a DECIMAL(10,6) column render with the zero, so a
    table migrated between the two scales - or to any other engine - reports
    every fractional key as only_in_a plus only_in_b. Planted: one value."""
    a = _duck(tmp_path, "a", "k decimal(6,6), v int", "(0.25, 1)", "(-0.5, 2)", side="A")
    b = _duck(tmp_path, "b", "k decimal(10,6), v int", "(0.25, 1)", "(-0.5, 3)")
    assert _found(_run(a, b, key="k")) == [("-0.5", "different", ["v"])]


@pytest.mark.postgres
def test_rt_hx1_duckdb_decimal_pp_key_against_postgres(pg_url, tmp_path):
    """RT-HX1 across engines: numeric(6,6) on PostgreSQL, decimal(6,6) on
    DuckDB, the same keys, one planted value difference."""
    import psycopg

    from parity.dialects.base import get_dialect

    con = psycopg.connect(pg_url, autocommit=True)
    name = f"rth_x_hx1_{os.getpid()}"
    try:
        con.execute(f"create table public.{name} (k numeric(6,6), v int)")
        con.execute(f"insert into public.{name} values (0.25, 1), (-0.5, 2)")
        b = _duck(tmp_path, "b", "k decimal(6,6), v int", "(0.25, 1)", "(-0.5, 3)")
        result = _run(get_dialect(pg_url, side="A"), b, key="k", ta=f"public.{name}")
        assert _found(result) == [("-0.5", "different", ["v"])]
    finally:
        con.execute(f"drop table if exists public.{name}")
        con.close()


@pytest.mark.snowflake
def test_rt_hx2_snowflake_number_p_0_matches_a_decimal_p_0(snowflake_url, tmp_path):
    """Fuzz seeds 32000004 and 32000014 (snowflake<->duckdb): NUMBER(14,0)
    and NUMBER(1,0) holding exactly what DuckDB's DECIMAL(14,0) holds were
    reported different on every row (`41886` against `41886.000000`), with
    only a type warning. Snowflake has no other way to store a DECIMAL(p,0)
    (its INTEGER *is* NUMBER(38,0)), so a PostgreSQL numeric(14,0) migrated
    to Snowflake can never compare clean. Planted: one text value."""
    from conftest import duckdb_write, open_duckdb, open_snowflake

    name = f"RTH_X_HX2_{os.getpid()}"
    w = open_snowflake(snowflake_url, side="A")
    cur = w._conn.cursor()
    try:
        cur.execute(
            f"create or replace table {name} (id integer, amount number(14,0), note varchar)"
        )
        cur.execute(f"insert into {name} values (1, 41886, 'x'), (2, 0, 'y')")
        path = str(tmp_path / "b.duckdb")
        con = duckdb_write(path)
        con.execute("create table t (id integer, amount decimal(14,0), note varchar)")
        con.execute("insert into t values (1, 41886, 'x'), (2, 0, 'z')")
        con.close()
        a = open_snowflake(snowflake_url, side="A")
        result = _run(a, open_duckdb(path), ta=name)
        # Side A's stored spelling (NOTE) names the column.
        found = [(k, kind, [c.lower() for c in cols]) for k, kind, cols in _found(result)]
        assert found == [(2, "different", ["note"])]
        assert any("compared by value" in w for w in result.warnings), result.warnings
    finally:
        cur.execute(f"drop table if exists {name}")
        cur.close()
        w.close()


def test_rt_hx1_decimal_38_38_values_keep_their_leading_zero():
    """RT-HX1 for values: at float_scale 38 DuckDB's rendering cast is
    decimal(38,38), which also prints `.5`; PostgreSQL prints `0.5`. The
    zero is restored (only at that scale). Planted: one value."""
    import duckdb

    from parity.dialects.duckdb_dialect import DuckDBDialect
    from parity.types import Column, LogicalType

    con = duckdb.connect()
    con.execute("create table t (i int, x decimal(38,38))")
    con.execute("insert into t values (1, 0.5), (2, -0.25), (3, 0), (4, null)")
    d = DuckDBDialect(float_scale=38)
    expr = d.normalize(Column("x", LogicalType.DECIMAL, "DECIMAL(38,38)"))
    got = dict(con.execute(f"select i, {expr} from t").fetchall())
    z = "0" * 38
    assert got == {1: "0.5" + z[1:], 2: "-0.25" + z[2:], 3: "0." + z, 4: "\\N"}
    # Planted: the neighbouring value renders differently.
    assert got[1] != got[2]


def test_rt_hx1_only_a_decimal_p_p_key_changes_its_sql():
    """The repair is scoped: DECIMAL(10,6) and DECIMAL(18,0) keys render the
    SQL they always did, so no other key checksum moves."""
    from parity.dialects.duckdb_dialect import DuckDBDialect
    from parity.types import Column, LogicalType

    d = DuckDBDialect()
    old = (
        "case when \"k\" = 0 then '0' when contains(cast(\"k\" as varchar), '.') "
        "then rtrim(rtrim(cast(\"k\" as varchar), '0'), '.') else cast(\"k\" as varchar) end"
    )
    for raw in ("DECIMAL(10,6)", "DECIMAL(18,0)", "DECIMAL(38,10)"):
        assert d.decimal_key_text('"k"', Column("k", LogicalType.DECIMAL, raw)) == old
    assert "starts_with" in d.decimal_key_text(
        '"k"', Column("k", LogicalType.DECIMAL, "DECIMAL(6,6)")
    )


@pytest.mark.parametrize(
    ("ddl_b", "rows_b", "found"),
    [
        # A faithful migration to a scale-0 decimal: only the planted note.
        (
            "decimal(14,0)",
            ["(1, 41886, 'x')", "(2, 0, 'z')", "(3, -7, 'w')"],
            [(2, "different", ["note"])],
        ),
        # Places: 41886.5 is not 41886, at any scale.
        (
            "decimal(14,2)",
            ["(1, 41886.5, 'x')", "(2, 0, 'y')", "(3, -7, 'w')"],
            [(1, "different", ["amount"])],
        ),
        # Within the float scale: 7e-7 rounds to 0.000001, never to zero.
        (
            "decimal(14,7)",
            ["(1, 41886, 'x')", "(2, 0.0000007, 'y')", "(3, -7, 'w')"],
            [(2, "different", ["amount"])],
        ),
        # NULL against 0 is a difference too.
        (
            "decimal(14,0)",
            ["(1, 41886, 'x')", "(2, null, 'y')", "(3, -7, 'w')"],
            [(2, "different", ["amount"])],
        ),
    ],
)
def test_rt_hx2_an_integer_column_compares_by_value_with_a_decimal(
    tmp_path, ddl_b, rows_b, found
):
    """RT-HX2 on one engine: bigint against a decimal compared `41886` with
    `41886.000000` and reported every row. Now both render as decimals."""
    a = _duck(
        tmp_path,
        "a",
        "id int, amount bigint, note varchar",
        "(1, 41886, 'x')",
        "(2, 0, 'y')",
        "(3, -7, 'w')",
        side="A",
    )
    b = _duck(tmp_path, "b", f"id int, amount {ddl_b}, note varchar", *rows_b)
    result = _run(a, b)
    assert _found(result) == found
    assert not result.identical
    (note,) = [w for w in result.warnings if w.startswith("column 'amount'")]
    assert "compared by value" in note and "BIGINT" in note


def test_rt_hx2_only_a_mixed_integer_decimal_pair_is_reconciled():
    """A same-type pair keeps its Columns, so its SQL - and every ordinary
    checksum - is what it was; INTEGER against FLOAT is left alone too."""
    from parity.engine import _integer_as_decimal
    from parity.types import Column, LogicalType

    i = Column("v", LogicalType.INTEGER, "BIGINT")
    d = Column("v", LogicalType.DECIMAL, "DECIMAL(14,0)")
    f = Column("v", LogicalType.FLOAT, "DOUBLE")
    assert _integer_as_decimal([i, d, f], [i, d, i]) == ([i, d, f], [i, d, i])
    a, b = _integer_as_decimal([i, d], [d, i])
    assert [c.logical_type for c in a + b] == [LogicalType.DECIMAL] * 4
    assert a[0].raw_type == "BIGINT" and b[1].raw_type == "BIGINT"


def test_rt_hx2_an_integer_key_against_a_decimal_key_is_not_warned(tmp_path):
    """A decimal key renders its minimal exact text, so integer 42 and
    decimal(18,0) 42 are one key - no 'every row will report' warning, and
    the planted deletion is the only difference."""
    a = _duck(tmp_path, "a", "id bigint, v int", "(1, 1)", "(2, 2)", "(3, 3)", side="A")
    b = _duck(tmp_path, "b", "id decimal(18,0), v int", "(1, 1)", "(3, 3)")
    result = _run(a, b)
    # Hashed (the types differ), so the key is reported as its text.
    assert _found(result) == [("2", "only_in_a", ["v"])]
    assert not any(w.startswith("key column") for w in result.warnings), result.warnings


def test_rt_hx2_bigquery_renders_an_int64_through_numeric():
    """An INT64 under the DECIMAL rule would ROUND to FLOAT64 and lose
    integers past 2^53; it goes through NUMERIC. A NUMERIC column's SQL is
    unchanged."""
    from parity.dialects.bigquery_dialect import BigQueryDialect
    from parity.types import Column, LogicalType

    d = BigQueryDialect()
    numeric = d.normalize(Column("v", LogicalType.DECIMAL, "NUMERIC(14, 0)"))
    assert "round(`v`, 6)" in numeric and "as numeric" not in numeric
    int64 = d.normalize(Column("v", LogicalType.DECIMAL, "INT64"))
    assert "round(cast(`v` as numeric), 6)" in int64


@pytest.mark.postgres
def test_rt_hx2_postgres_bigint_against_duckdb_decimal(pg_url, tmp_path):
    """RT-HX2 across engines: PostgreSQL bigint against DuckDB decimal(18,0),
    PostgreSQL numeric(14,0) against DuckDB BIGINT (past 2^53 too), the same
    values, one planted value difference."""
    import psycopg

    from parity.dialects.base import get_dialect

    con = psycopg.connect(pg_url, autocommit=True)
    name = f"rth_x_hx2_{os.getpid()}"
    try:
        con.execute(f"create table public.{name} (id int, a bigint, b numeric(18,0))")
        con.execute(
            f"insert into public.{name} values (1, 41886, 9007199254740993), (2, -7, 0)"
        )
        d = _duck(
            tmp_path,
            "b",
            "id int, a decimal(18,0), b bigint",
            "(1, 41886, 9007199254740993)",
            "(2, -7, 1)",
        )
        result = _run(get_dialect(pg_url, side="A"), d, ta=f"public.{name}")
        assert _found(result) == [(2, "different", ["b"])]
    finally:
        con.execute(f"drop table if exists public.{name}")
        con.close()
