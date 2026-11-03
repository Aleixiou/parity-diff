"""Red team: live cross-engine differential fuzzing against a brute-force oracle.

Unlike `test_properties.py` (an in-memory dialect) and `test_fuzz_encoding.py`
(one value, two engines, same text?), this drives the *whole* `diff` across
every pair of live engines and checks its verdict against an oracle that never
touches the tool's SQL: both tables are fetched in full through the raw drivers
and compared by value in Python. See `fuzz_harness.py` for what is generated
and mutated.

Fast by default (`PARITY_REDTEAM_CASES`, 30 per engine pair). A deep run:

    PARITY_REDTEAM_CASES=400 PARITY_REDTEAM_SEED=7 pytest tests/redteam -q

or, with a per-pair tally and shrunk repros printed, `python
tests/redteam/run_fuzz.py --cases 400`. SQL Server joins when
PARITY_TEST_MSSQL is set; every pair skips when one of its engines is not
reachable. Tables are named `rt_fz_*` and dropped after each case.

The findings are pinned below as strict xfails, each a minimal repro: RT-E1/E2
were found and shrunk by the fuzzer (and are excluded from generation by
`KNOWN` in the harness, so a failure of the fuzz test itself is always
something new); RT-E8 and RT-E10 were also found by the fuzzer and are kept
out of generation by `_mysql_key_fold` and a DECIMAL-as-DOUBLE precision cap;
the rest came from probes of values the generator cannot produce - Python has
no `infinity` or BC date, and no driver binds an unpaired UTF-16 surrogate
(RT-E9) - or of key shapes it holds fixed.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fuzz_harness as fh
from conftest import (
    MSSQL_URL,
    MYSQL_URL,
    PG_URL,
    _mssql_available,
    _mysql_available,
    _pg_available,
)

from parity.dialects.base import get_dialect
from parity.engine import diff

CASES = int(os.environ.get("PARITY_REDTEAM_CASES", "30"))
SEED = int(os.environ.get("PARITY_REDTEAM_SEED", "1"))
ENGINES = ("postgres", "duckdb", "mysql", "mssql")


def _available(name: str) -> tuple[bool, str]:
    """Can this engine be used here, and if not, why not."""
    if name == "duckdb":
        try:
            import duckdb  # noqa: F401
        except ImportError:  # pragma: no cover - depends on install extras
            return False, "duckdb is not installed"
        return True, ""
    return {
        "postgres": _pg_available,
        "mysql": _mysql_available,
        "mssql": _mssql_available,
    }[name]()


@pytest.fixture(scope="module")
def engines():
    """One writer per reachable engine, shared by every case in the module."""
    urls = {"postgres": PG_URL, "mysql": MYSQL_URL, "mssql": MSSQL_URL, "duckdb": ""}
    opened: dict[str, fh.Engine] = {}
    why: dict[str, str] = {}
    for name in ENGINES:
        ok, reason = _available(name)
        if ok:
            opened[name] = fh.open_engine(name, urls)
        else:
            why[name] = reason
    yield opened, why
    for e in opened.values():
        e.close()


@pytest.mark.parametrize(("a", "b"), fh.pairs(ENGINES), ids=lambda x: x)
def test_live_diff_agrees_with_the_brute_force_oracle(engines, a, b):
    """Every case: exactly the oracle's differences, never fewer, never more,
    and "identical" if and only if the oracle found none."""
    opened, why = engines
    for name in (a, b):
        if name not in opened:
            pytest.skip(f"{name}: {why.get(name)}")
    failures = []
    with_diffs = 0
    for i in range(CASES):
        # Stable across processes (unlike hash()), so a reported seed reproduces.
        seed = SEED * 1_000_000 + fh.pairs(ENGINES).index((a, b)) * 10_000 + i
        ea, eb = (a, b) if i % 2 else (b, a)
        case = fh.random_case(seed, ea, eb)
        out = fh.run_case(case, opened)
        with_diffs += bool(out.expected)
        if not out.ok:
            failures.append(out)
    if failures:
        first = failures[0]
        verdict = first.verdict()
        if verdict != "ERROR":
            small = fh.shrink(first.case, opened, lambda o: o.verdict() == verdict)
            first = fh.run_case(small, opened)
        pytest.fail(
            f"{len(failures)}/{CASES} cases disagree with the oracle; first, shrunk:\n"
            f"[{first.verdict()}] {first.case.describe()}\n"
            f"error={first.error}\nmissed={sorted(map(repr, first.missed))[:5]}\n"
            f"extra={sorted(map(repr, first.extra))[:5]}\n"
            f"rows_a={first.case.rows_a!r}"[:3000]
            + f"\nrows_b={first.case.rows_b!r}"[:3000]
        )
    # The fuzzer is only worth something if it plants differences: a run where
    # every case was identical would pass whatever the tool did.
    if CASES >= 20:
        assert with_diffs >= CASES // 5, (
            f"only {with_diffs}/{CASES} cases had a difference"
        )


def test_the_harness_catches_a_planted_false_match(monkeypatch):
    """The oracle is independent: with a known trigger enabled it must fail.

    Guards the fuzzer itself. If this ever passes cleanly, the oracle has
    started agreeing with the tool rather than with the data. DuckDB on both
    sides, so it runs with no server at all.

    The trigger, RT-E1, is fixed - text is escaped, so `\\N` is no longer
    NULL - so the fix is switched off here: with `escape_text` an identity,
    the tool has the old bug again and the oracle must catch it.
    """
    from parity.dialects.duckdb_dialect import DuckDBDialect

    monkeypatch.setattr(DuckDBDialect, "escape_text", lambda self, expr: expr)
    pair = {"duckdb": fh.Duck(), "duckdb2": fh.Duck()}
    verdicts = []
    for i in range(60):
        case = fh.random_case(900_000 + i, "duckdb", "duckdb2", {"sentinel_text"})
        if any(":sentinel@" in m for m in case.mutations):
            verdicts.append(fh.run_case(case, pair).verdict())
        if len(verdicts) >= 5:
            break
    assert verdicts, "no case planted the trigger"
    assert {"FALSE MATCH", "MISSED DIFF"} & set(verdicts), verdicts


# ------------------------------------------------------------------ findings
#
# Minimal repros the fuzzer and the follow-up probes shrank to. Each asserts
# the *correct* behaviour, so it fails today and the strict xfail turns into
# an XPASS - a hard failure - the day the finding is fixed, prompting removal.


def _duck(tmp_path, name: str, ddl: str, *rows: str, side: str = "B"):
    """A DuckDB file holding one table `t`, opened read-only as a dialect."""
    import duckdb

    path = str(tmp_path / f"rt_fz_{name}.duckdb")
    con = duckdb.connect(path)
    con.execute(f"create table t ({ddl})")
    for r in rows:
        con.execute(f"insert into t values {r}")
    con.close()
    return get_dialect("duckdb:///" + path.replace("\\", "/"), side=side)


def _run(a, b, key="id", ta="t", tb="t"):
    """`diff` with default knobs, closing both sides whatever happens."""
    try:
        return diff(a, b, ta, tb, key=key)
    finally:
        a.close()
        b.close()


def _found(result) -> list[tuple]:
    """The result's differences as (key, kind, columns) triples."""
    return [(d.key, d.kind, d.columns) for d in result.diffs]


def test_rt_e1_the_text_backslash_n_is_not_null(tmp_path):
    """RT-E1: NULL and the two characters backslash-N must differ."""
    a = _duck(tmp_path, "a", "id int, note varchar", "(1, NULL)", side="A")
    b = _duck(tmp_path, "b", "id int, note varchar", "(1, '\\N')")
    assert _found(_run(a, b)) == [(1, "different", ["note"])]


def test_rt_e1_a_null_key_part_is_not_the_text_backslash_n(tmp_path):
    """RT-E1 in a key: (1, NULL) and (1, backslash-N) are two different rows."""
    a = _duck(tmp_path, "a", "id int, k2 varchar, v int", "(1, NULL, 7)", side="A")
    b = _duck(tmp_path, "b", "id int, k2 varchar, v int", "(1, '\\N', 7)")
    # Fixed by refusing the NULL key part (RT-B2): it can match nothing.
    with pytest.raises(ValueError, match="NULL"):
        _run(a, b, key=["id", "k2"])


def test_rt_e2_the_field_separator_inside_text_is_not_a_boundary(tmp_path):
    """RT-E2: moving chr(31) across a column boundary must be seen."""
    ddl = "id int, c1 varchar, c2 varchar"
    a = _duck(tmp_path, "a", ddl, "(1, 'p' || chr(31), 'q')", side="A")
    b = _duck(tmp_path, "b", ddl, "(1, 'p', chr(31) || 'q')")
    assert _found(_run(a, b)) == [(1, "different", ["c1", "c2"])]


def test_rt_e6_an_integral_decimal_key_matches_an_integer_key(tmp_path):
    """RT-E6: DECIMAL(10,0) and BIGINT keys holding the same integers match,
    or a warning says the key types differ."""
    a = _duck(tmp_path, "a", "id decimal(10,0), v int", "(1, 1)", "(2, 2)", side="A")
    b = _duck(tmp_path, "b", "id bigint, v int", "(1, 1)", "(2, 2)")
    result = _run(a, b)
    key_warned = any("key" in w and " type" in w for w in result.warnings)
    assert result.identical or key_warned


def test_rt_e7_nanosecond_timestamps_are_compared_or_caveated(tmp_path):
    """RT-E7: TIMESTAMP_NS differences below a microsecond are seen or caveated."""
    ddl = "id int, t timestamp_ns"
    a = _duck(tmp_path, "a", ddl, "(1, '2024-01-01 00:00:00.123456789')", side="A")
    b = _duck(tmp_path, "b", ddl, "(1, '2024-01-01 00:00:00.123456001')")
    result = _run(a, b)
    caveated = any("second" in w.lower() for w in result.warnings)
    assert not result.identical or caveated


@pytest.fixture
def pg_table(pg_url):
    """Create PostgreSQL tables named rt_fz_* in public, dropped afterwards."""
    import psycopg

    made: list[str] = []
    con = psycopg.connect(pg_url, autocommit=True)

    def make(name: str, ddl: str, *rows: str) -> str:
        """Create table `name` with `ddl` and `rows`, remembered for the drop."""
        con.execute(f"drop table if exists public.{name}")
        con.execute(f"create table public.{name} ({ddl})")
        for r in rows:
            con.execute(f"insert into public.{name} values {r}")
        made.append(name)
        return f"public.{name}"

    yield make
    for name in made:
        con.execute(f"drop table if exists public.{name}")
    con.close()


@pytest.mark.postgres
def test_rt_e3_postgres_infinity_is_not_null(pg_url, pg_table, tmp_path):
    """RT-E3: PostgreSQL infinity dates must not equal NULL."""
    ddl = "id int, d date, ts timestamp"
    t = pg_table("rt_fz_inf", ddl, "(1, 'infinity', '-infinity')")
    b = _duck(tmp_path, "b", ddl, "(1, NULL, NULL)")
    assert not _run(get_dialect(pg_url, side="A"), b, ta=t).identical


@pytest.mark.postgres
def test_rt_e3b_infinity_on_both_sides_is_identical(pg_url, pg_table, tmp_path):
    """RT-E3b: infinity on PostgreSQL and on DuckDB must compare equal."""
    t = pg_table("rt_fz_inf2", "id int, d date", "(1, 'infinity')")
    b = _duck(tmp_path, "b", "id int, d date", "(1, 'infinity')")
    assert _run(get_dialect(pg_url, side="A"), b, ta=t).identical


@pytest.mark.postgres
def test_rt_e4_a_bc_date_is_not_the_ad_date(pg_url, pg_table, tmp_path):
    """RT-E4: a BC date must not equal the AD date with the same digits."""
    t = pg_table("rt_fz_bc", "id int, d date", "(1, '0044-03-15 BC')")
    b = _duck(tmp_path, "b", "id int, d date", "(1, '0044-03-15')")
    assert not _run(get_dialect(pg_url, side="A"), b, ta=t).identical


@pytest.mark.postgres
def test_rt_e4b_a_bc_date_on_both_sides_is_identical(pg_url, pg_table, tmp_path):
    """RT-E4b: one BC date on PostgreSQL and on DuckDB must compare equal."""
    t = pg_table("rt_fz_bc2", "id int, d date", "(1, '0044-03-15 BC')")
    b = _duck(tmp_path, "b", "id int, d date", "(1, '0044-03-15 (BC)')")
    assert _run(get_dialect(pg_url, side="A"), b, ta=t).identical


@pytest.mark.mysql
@pytest.mark.postgres
def test_rt_e5_mysql_case_distinct_text_keys_are_unique(mysql_url, pg_url, pg_table):
    """RT-E5, fixed: MySQL counted text keys unique under utf8mb4_0900_ai_ci,
    so 'a'/'A' and 'e'/'é' were refused as duplicates. It now counts bytes
    (`MySQLDialect.count_distinct_key`)."""
    my = fh.MySQL(mysql_url)
    try:
        my.exec("drop table if exists rt_fz_ci")
        my.exec("create table rt_fz_ci (id varchar(20), v int)")
        my.exec("insert into rt_fz_ci values ('a', 1), ('A', 2), ('e', 3), ('é', 4)")
        rows = ("('a', 1)", "('A', 2)", "('e', 3)", "('é', 5)")
        t = pg_table("rt_fz_ci", "id text, v int", *rows)
        a = get_dialect(mysql_url, side="A")
        result = _run(a, get_dialect(pg_url, side="B"), ta="rt_fz_ci", tb=t)
        assert [(d.key, d.kind) for d in result.diffs] == [("é", "different")]
    finally:
        my.exec("drop table if exists rt_fz_ci")
        my.close()


@pytest.mark.mysql
def test_rt_e8_mysql_composite_integer_keys_are_unique(mysql_url, tmp_path):
    """RT-E8, fixed: under utf8mb4_0900_ai_ci chr(31) is ignorable, so the
    composite keys (5, 944) and (59, 44) counted as one key and the diff was
    refused. Fuzz seed 1030004 (mysql<->duckdb, bigint+int key) shrank to it."""
    my = fh.MySQL(mysql_url)
    try:
        my.exec("drop table if exists rt_fz_ck")
        my.exec("create table rt_fz_ck (k0 bigint, k1 int, v int)")
        my.exec("insert into rt_fz_ck values (5, 944, 1), (59, 44, 2)")
        b = _duck(tmp_path, "b", "k0 bigint, k1 int, v int", "(5, 944, 1)", "(59, 44, 3)")
        result = _run(
            get_dialect(mysql_url, side="A"), b, key=["k0", "k1"], ta="rt_fz_ck"
        )
        assert [d.kind for d in result.diffs] == ["different"]
    finally:
        my.exec("drop table if exists rt_fz_ck")
        my.close()


@pytest.fixture
def mssql_table(mssql_url):
    """Create SQL Server tables named rt_fz_* in dbo, dropped afterwards."""
    ms = fh.MSSQL(mssql_url)
    made: list[str] = []

    def make(name: str, ddl: str, *rows: str) -> str:
        """Create table `name` with `ddl` and `rows`, remembered for the drop."""
        ms.exec(f"drop table if exists dbo.{name}")
        ms.exec(f"create table dbo.{name} ({ddl})")
        for r in rows:
            ms.exec(f"insert into dbo.{name} values {r}")
        made.append(name)
        return f"dbo.{name}"

    yield make
    for name in made:
        ms.exec(f"drop table if exists dbo.{name}")
    ms.close()


@pytest.mark.mssql
def test_rt_e9_distinct_lone_surrogates_are_different(mssql_url, mssql_table):
    """RT-E9, fixed: the UTF-8 conversion before HASHBYTES turned every
    unpaired surrogate into U+FFFD, so two different values - and a real
    U+FFFD - hashed alike. Invalid UTF-16 now renders as a marker plus its
    own bytes (`MSSQLDialect._utf16_safe`)."""
    ddl = "id int, v nvarchar(10)"
    ta = mssql_table("rt_fz_sur_a", ddl, "(1, nchar(0xD800))")
    tb = mssql_table("rt_fz_sur_b", ddl, "(1, nchar(0xD801))")
    a, b = get_dialect(mssql_url, side="A"), get_dialect(mssql_url, side="B")
    assert not _run(a, b, ta=ta, tb=tb).identical


@pytest.mark.mssql
def test_rt_e9b_a_lone_surrogate_row_can_be_reported(mssql_url, mssql_table, tmp_path):
    """RT-E9b, fixed: downloading such a row raised UnicodeDecodeError in the
    driver. The marker is plain ASCII, so the row downloads and is reported -
    and against a real U+FFFD the text column now differs too."""
    ta = mssql_table(
        "rt_fz_sur_c", "id int, v nvarchar(10), w int", "(1, nchar(0xD800), 1)"
    )
    b = _duck(tmp_path, "b", "id int, v varchar, w int", "(1, chr(65533), 2)")
    result = _run(get_dialect(mssql_url, side="A"), b, ta=ta)
    assert [(d.key, d.kind, sorted(d.columns)) for d in result.diffs] == [
        (1, "different", ["v", "w"])
    ]
    assert result.diffs[0].values_a["v"].startswith("parity: invalid UTF-16 0x00D8")


def test_rt_e10_the_huge_float_refusal_names_side_and_column(tmp_path):
    """RT-E10, fixed: the |x| >= 2^116 refusal was a raw driver error naming
    neither side nor column (DuckDB, PostgreSQL), or a bare 'DOUBLE value is
    out of range in exp(...)' (MySQL). Fuzz seeds 1300352 and 1301110."""
    a = _duck(tmp_path, "a", "id int, amount double", "(1, 1e35)", side="A")
    b = _duck(tmp_path, "b", "id int, amount double", "(1, 1.0)")
    with pytest.raises(Exception) as info:
        _run(a, b)
    assert "side A" in str(info.value) and "amount" in str(info.value)


@pytest.mark.parametrize("engine", ["postgres", "mysql", "mssql"])
def test_rt_e10_live_engines_name_side_column_and_value(engine):
    """RT-E10 on each server engine: side, column (quote, backslash and all)
    and value in one error, against DuckDB on the other side."""
    ok, why = _available(engine)
    if not ok:
        pytest.skip(why)
    urls = {"postgres": PG_URL, "mysql": MYSQL_URL, "mssql": MSSQL_URL, "duckdb": ""}
    opened = {engine: fh.open_engine(engine, urls), "duckdb": fh.Duck()}
    try:
        name = "o'k\b"
        case = fh.Case(
            seed=0,
            a_engine=engine,
            b_engine="duckdb",
            key=[fh.Col("id", "int")],
            cols=[fh.Col(name, "double")],
            rows_a=[(1, 1e35)],
            rows_b=[(1, 1.0)],
        )
        error = fh.run_case(case, opened).error or ""
    finally:
        for e in opened.values():
            e.close()
    assert error.startswith(f"ValueError: [side A: {engine}] parity: FLOAT value"), error
    assert f"in column {name!r}" in error, error
