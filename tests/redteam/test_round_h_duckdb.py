"""Red team round H: DuckDB, every surface.

Every test plants a difference and asserts exactly it is found, or asserts a
refusal. Confirmed problems are `xfail(strict=True)` with an `RT-HD<n>` id and
assert what a trustworthy tool must do, so they turn green when fixed.

Areas: the database file itself (macros that shadow built-ins, SQLite files,
WAL, locks, read-only), every native type including nested ones (STRUCT, MAP,
UNION, fixed-size ARRAY) and the pairs of different values that might render
alike, every key type, `--where` against DuckDB's grammar (list literals,
`E''` strings, lambdas), connection strings and the CLI on Windows paths,
DuckDB library versions 1.0-2.0, and scale.

All files are written under pytest's tmp_path; nothing outside it is touched.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import open_duckdb

from parity.cli import main
from parity.dialects.base import get_dialect, row_filter_problem

from .rtg_servers import duck_file, found, run_diff

duckdb = pytest.importorskip("duckdb")

DUCK_VERSION = tuple(int(p) for p in duckdb.__version__.split(".")[:2])


def pair(tmp_path: Path, typ: str, a: str, b: str, *, key: str = "int") -> tuple[str, str]:
    """Two files of `t(id, v typ)`: row 1 holds `a` on side A and `b` on side B;
    row 2 holds `a` on both; row 3 is NULL on both."""
    ddl = f"create table t (id {key}, v {typ})"
    pa = duck_file(tmp_path, "pa", ddl, f"insert into t values (1, {a}), (2, {a}), (3, NULL)")
    pb = duck_file(tmp_path, "pb", ddl, f"insert into t values (1, {b}), (2, {a}), (3, NULL)")
    return pa, pb


def diff_pair(tmp_path: Path, typ: str, a: str, b: str, **kw):
    """`pair`, compared."""
    pa, pb = pair(tmp_path, typ, a, b)
    return run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t", **kw)


def run_cli(*argv: str) -> tuple[int, str, str]:
    """Drive the real CLI in-process. Returns (code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


ONE = [(1, "different", ("v",))]


# =========================================================== the database file


#: Functions the dialect's SQL calls, each shadowed by a macro stored in the
#: file. DuckDB resolves `main` macros before the built-ins, for a read-only
#: connection too, and keeps them in the file.
SHADOWS = {
    "md5": "create macro md5(x) as 'd41d8cd98f00b204e9800998ecf8427e'",
    "substr": "create macro substr(x, a, b) as '000000000000000'",
    "replace": "create macro replace(s, a, b) as 'r'",
    "concat_ws": "create macro concat_ws(s, a, b) as 'zz'",
}


@pytest.mark.sample_check_may_disagree
@pytest.mark.xfail(
    strict=True,
    reason="RT-HD1: a macro in the DuckDB file named like a built-in (md5, substr, "
    "replace, concat_ws) replaces it in parity's SQL - every row hashes alike and a "
    "changed row is a false match; nothing pins the built-ins (cf. PostgreSQL's "
    "search_path pin)",
)
@pytest.mark.parametrize("name", sorted(SHADOWS))
def test_a_macro_shadowing_a_builtin_cannot_hide_a_change(tmp_path, name):
    """Both files carry the same macro library (a team's shared helpers, or a
    hostile file); side B changed one row of 50,000. The tool must find it or
    refuse the file - never call the tables identical."""
    rows = "insert into t select i, 'v' || i from range(1, 50001) r(i)"
    ddl = "create table t (id int, v varchar)"
    pa = duck_file(tmp_path, "a", ddl, rows, SHADOWS[name])
    pb = duck_file(tmp_path, "b", ddl, rows, "update t set v = 'changed' where id = 4242", SHADOWS[name])
    try:
        result = run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t")
    except ValueError:
        return  # a refusal is an acceptable answer
    assert not result.identical
    assert found(result) == [(4242, "different", ("v",))]


def test_a_macro_on_one_side_only_does_not_hide_a_change(tmp_path):
    """The shadowing macro on one side only: its hashes differ from the honest
    side's, so the walk cannot call the tables identical (it reports noise)."""
    rows = "insert into t select i, 'v' || i from range(1, 2001) r(i)"
    ddl = "create table t (id int, v varchar)"
    pa = duck_file(tmp_path, "a", ddl, rows)
    pb = duck_file(tmp_path, "b", ddl, rows, "update t set v = 'x' where id = 7", SHADOWS["md5"])
    result = run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t")
    assert not result.identical
    assert (7, "different", ("v",)) in found(result)


def test_user_types_cannot_shadow_builtin_types(tmp_path):
    """`create type varchar as integer` is refused by DuckDB itself, so the
    dialect's casts cannot be redirected the way its functions can."""
    con = duckdb.connect(str(tmp_path / "ty.duckdb"))
    try:
        for t in ("varchar", "hugeint", "bigint", "decimal", "timestamp", "double"):
            with pytest.raises(duckdb.Error, match="already exists"):
                con.execute(f"create type {t} as integer")
    finally:
        con.close()


def _sqlite_file(tmp_path: Path) -> str:
    """A SQLite database whose name looks like any DuckDB file."""
    path = str(tmp_path / "warehouse.db")
    con = sqlite3.connect(path)
    con.execute("create table t (id integer, v text)")
    con.execute("insert into t values (1, 'a')")
    con.commit()
    con.close()
    return path


@pytest.mark.xfail(
    strict=True,
    reason="RT-HD4: duckdb:/// on a SQLite file auto-installs (downloads from "
    "extensions.duckdb.org) and loads sqlite_scanner inside duckdb.connect, before "
    "the dialect turns autoinstall/autoload off - a network call (CLAUDE.md 6)",
)
def test_a_sqlite_file_is_refused_without_loading_an_extension(tmp_path):
    """Verified 2026-11-04 with HOME pointed at an empty directory: the connect
    wrote `.duckdb/extensions/v1.5.5/windows_amd64/sqlite_scanner.duckdb_extension`
    there - downloaded during `get_dialect`. The dialect must refuse a file
    that is not a DuckDB database, naming the side, before any extension loads."""
    path = _sqlite_file(tmp_path)
    loaded: list = []
    try:
        d = get_dialect(f"duckdb:///{path}", side="A")
    except ValueError as exc:
        assert "[side A" in str(exc)
        return
    except duckdb.Error as exc:  # raw driver error: no side, maybe a download attempt
        pytest.fail(f"raw driver error, no side named: {exc}")
    try:
        loaded = d.query(
            "select extension_name from duckdb_extensions() "
            "where loaded and install_path <> '(BUILT-IN)'"
        )
    finally:
        d.close()
    pytest.fail(f"a SQLite file opened as a DuckDB side; extensions loaded: {loaded}")


def test_a_wal_left_by_a_dead_writer_is_replayed_read_only(tmp_path):
    """A writer killed before its checkpoint leaves a WAL. The read-only side
    must see the committed changes (the update and the delete in the WAL) and
    leave the WAL in place."""
    path = str(tmp_path / "wal.duckdb")
    code = (
        "import duckdb, os, sys\n"
        "c = duckdb.connect(sys.argv[1])\n"
        "c.execute(\"set checkpoint_threshold = '1TB'\")\n"
        "c.execute('create table t (id int, v varchar)')\n"
        "c.execute(\"insert into t select i, 'v' || i from range(1, 1001) r(i)\")\n"
        "c.execute('checkpoint')\n"
        "c.execute(\"update t set v = 'walchange' where id = 5\")\n"
        "c.execute('delete from t where id = 6')\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", code, path], check=True)
    assert os.path.exists(path + ".wal")
    ref = duck_file(
        tmp_path,
        "ref",
        "create table t (id int, v varchar)",
        "insert into t select i, 'v' || i from range(1, 1001) r(i)",
    )
    result = run_diff(open_duckdb(path, "A"), open_duckdb(ref, "B"), "main.t", "main.t")
    assert found(result) == [(5, "different", ("v",)), (6, "only_in_b", ("v",))]
    assert os.path.exists(path + ".wal")


def test_a_file_held_by_a_writer_process_names_the_side(tmp_path):
    """Another process holds the file read-write (DuckDB's exclusive lock on
    Windows and POSIX alike): a clear refusal naming the side and the file."""
    ref = duck_file(tmp_path, "held", "create table t (id int)", "insert into t values (1)")
    code = "import duckdb, sys, time\nc = duckdb.connect(sys.argv[1])\nprint('ok', flush=True)\ntime.sleep(60)\n"
    writer = subprocess.Popen([sys.executable, "-c", code, ref], stdout=subprocess.PIPE, text=True)
    try:
        assert writer.stdout.readline().strip() == "ok"
        with pytest.raises(ValueError, match=r"^\[side B: duckdb\] could not connect") as exc:
            get_dialect(f"duckdb:///{ref}", side="B")
        assert "held.duckdb" in str(exc.value)
    finally:
        writer.kill()
        writer.wait()


def test_the_side_is_read_only_even_for_a_writable_file(tmp_path):
    """No statement the dialect can be made to run writes the file."""
    ref = duck_file(tmp_path, "ro", "create table t (id int)", "insert into t values (1)")
    d = open_duckdb(ref, "A")
    try:
        for sql in (
            "insert into t values (2)",
            "create table u (x int)",
            "create macro m(x) as x",
            "create view w as select 1",
        ):
            with pytest.raises(duckdb.Error):
                d.query(sql)
    finally:
        d.close()
    con = duckdb.connect(ref, read_only=True)
    try:
        assert con.execute("select count(*) from t").fetchone() == (1,)
    finally:
        con.close()


# ================================================================ value pairs

#: (type, value on side A, value on side B): two different stored values that
#: a careless rendering could print alike. Each must be found, exactly.
FOUND_PAIRS = [
    ("UHUGEINT", "340282366920938463463374607431768211455", "340282366920938463463374607431768211454"),
    ("UHUGEINT", "0", "1"),
    ("HUGEINT", "-170141183460469231731687303715884105728", "-170141183460469231731687303715884105727"),
    ("UBIGINT", "18446744073709551615", "18446744073709551614"),
    ("UTINYINT", "255", "254"),
    ("USMALLINT", "65535", "0"),
    ("UINTEGER", "4294967295", "4294967294"),
    ("DECIMAL(38,0)", "99999999999999999999999999999999", "99999999999999999999999999999998"),
    ("DECIMAL(1,0)", "1", "2"),
    ("DECIMAL(1,1)", "0.1", "0.2"),
    ("DOUBLE", "'inf'::double", "'-inf'::double"),
    ("DOUBLE", "'nan'::double", "'inf'::double"),
    ("TIME", "'23:59:59.999999'", "'24:00:00'"),
    ("TIME_NS", "'12:00:00.000000001'", "'12:00:00.000000002'"),
    ("TIMETZ", "'12:00:00+00'", "'12:00:00+01'"),
    ("TIMESTAMP", "'infinity'", "'294247-01-10 04:00:54.775806'"),
    ("TIMESTAMP", "'-infinity'", "'290309-12-22 (BC) 00:00:00'"),
    ("DATE", "'infinity'", "'5881580-07-10'"),
    ("DATE", "'-infinity'", "'5877642-06-25 (BC)'"),
    ("DATE", "'0001-01-01'", "'0001-01-01 (BC)'"),
    ("TIMESTAMPTZ", "'infinity'", "'-infinity'"),
    ("TIMESTAMP_S", "'0001-01-01 00:00:00'", "'0001-01-01 00:00:01'"),
    ("TIMESTAMP_MS", "'9999-12-31 23:59:59.999'", "'9999-12-31 23:59:59.998'"),
    ("TIMESTAMP_NS", "'1677-09-22 00:00:00.000000001'", "'1677-09-22 00:00:00.000000002'"),
    ("INTERVAL", "interval '1 month'", "interval '30 days'"),
    ("INTERVAL", "interval '1 day'", "interval '24 hours'"),
    ("INTERVAL", "interval '-1 month 1 day'", "interval '1 month -1 day'"),
    ("INTERVAL", "to_microseconds(1)", "to_microseconds(2)"),
    ("VARCHAR", "'a'", "'a' || chr(0)"),
    ("VARCHAR", "chr(0)", "''"),
    ("VARCHAR", "'\\N'", "NULL"),
    ("VARCHAR", "'é'", "'e' || chr(769)"),
    ("VARCHAR", "'a' || chr(31) || 'b'", "'a\\sb'"),
    ("VARCHAR", "chr(8238) || 'abc'", "'abc'"),  # RTL override
    ("VARCHAR", "chr(1114111)", "chr(65533)"),  # last code point vs replacement char
    ("VARCHAR", "' '", "''"),
    ("VARCHAR COLLATE NOCASE", "'a'", "'A'"),
    ("VARCHAR COLLATE NOACCENT", "'e'", "'é'"),
    ("VARCHAR COLLATE NFC", "'é'", "'e' || chr(769)"),
    ("VARCHAR COLLATE de", "'ss'", "'ß'"),
    ("VARCHAR COLLATE NOCASE.NOACCENT", "'E'", "'é'"),
    ("BLOB", "'\\x00'::blob", "''::blob"),
    ("BLOB", "'a'::blob", "'A'::blob"),
    ("BIT", "'0'::bit", "'00'::bit"),
    ("UUID", "'00000000-0000-0000-0000-000000000000'", "'00000000-0000-0000-0000-000000000001'"),
    ("UUID", "'ffffffff-ffff-ffff-ffff-ffffffffffff'", "'7fffffff-ffff-ffff-ffff-ffffffffffff'"),
    ("JSON", "'{\"a\":1}'", "'{\"a\":1.0}'"),
    ("JSON", "'\"\\u00e9\"'", "'\"é\"'"),
    ("JSON", "'{\"a\":1,\"a\":2}'", "'{\"a\":2}'"),
    ("ENUM('a', 'b', 'a ')", "'a'", "'a '"),
    ("VARCHAR[]", "['a, b']", "['a', 'b']"),
    ("VARCHAR[]", "[NULL]", "['NULL']"),
    ("VARCHAR[]", "[]", "['']"),
    ("VARCHAR[]", "NULL", "[]"),
    ("VARCHAR[]", "['\\,']", "[',']"),
    ("VARCHAR[]", "['a\\,', 'b']", "['a', '\\,b']"),
    ("INTEGER[][]", "[[1, 2], [3]]", "[[1], [2, 3]]"),
    ("VARCHAR[][]", "[['a'], ['b']]", "[['a', 'b']]"),
    ("VARCHAR[][]", "[[NULL]]", "[['NULL']]"),
    ("VARCHAR[][2]", "[['a, b'], ['c']]", "[['a', 'b'], ['c']]"),
    ("INTEGER[2]", "[1, 2]", "[1, 3]"),
    ("DOUBLE[2]", "[0.1, 2]", "[0.1000000000000001, 2]"),
    ("FLOAT[3]", "[1.0, 2.0, 3.0]", "[1.0000001, 2.0, 3.0]"),
    ("BOOLEAN[2]", "[true, NULL]", "[true, false]"),
    ("UUID[2]", "[NULL, NULL]", "[NULL, '00000000-0000-0000-0000-000000000000']"),
    ("TIME[]", "['24:00:00']", "['00:00:00']"),
    ("INTERVAL[]", "[interval '1 month']", "[interval '30 days']"),
    ("BLOB[]", "['\\x00'::blob]", "[''::blob]"),
    ("JSON[]", "['{\"a\": 1}']", "['{\"a\":1}']"),
    ("BOOLEAN[]", "[NULL]", "[false]"),
    ("HUGEINT[]", "[170141183460469231731687303715884105727]", "[170141183460469231731687303715884105726]"),
    ("UHUGEINT[]", "[340282366920938463463374607431768211455]", "[340282366920938463463374607431768211454]"),
    ("TIMETZ[]", "['12:00:00+05:00:30']", "['12:00:00+05:30']"),
    ("DATE[]", "['infinity']", "['9999-12-31']"),
    ("BIT[]", "['01'::bit]", "['1'::bit]"),
    ("STRUCT(a VARCHAR, b VARCHAR)", "{'a': 'x'', ''b'': ''y', 'b': 'z'}", "{'a': 'x', 'b': 'y'', ''b'': ''z'}"),
    ("STRUCT(a VARCHAR, b VARCHAR)", "{'a': NULL, 'b': 'z'}", "{'a': 'NULL', 'b': 'z'}"),
    ("STRUCT(a VARCHAR)", "{'a': NULL}", "NULL"),
    ("STRUCT(a VARCHAR)", "{'a': '\\'}", "{'a': '\\\\'}"),
    ("STRUCT(a DOUBLE)", "{'a': 0.1}", "{'a': 0.1000000000000001}"),
    ("STRUCT(a TIMESTAMP_NS)", "{'a': '2020-01-01 00:00:00.000000001'}", "{'a': '2020-01-01 00:00:00.000000002'}"),
    ("STRUCT(a INTERVAL)", "{'a': interval '1 month'}", "{'a': interval '30 days'}"),
    ("STRUCT(a DECIMAL(38,20))", "{'a': 0.00000000000000000001}", "{'a': 0.00000000000000000002}"),
    ("STRUCT(a BLOB)", "{'a': '\\x00'::blob}", "{'a': '\\x5Cx00'::blob}"),
    ("STRUCT(a TIME)", "{'a': '24:00:00'::time}", "{'a': '00:00:00'::time}"),
    ("STRUCT(a DATE)", "{'a': 'infinity'::date}", "{'a': '5881580-07-10'::date}"),
    ("STRUCT(\"a, b\" INTEGER, c INTEGER)", "{'a, b': 1, 'c': 2}", "{'a, b': 1, 'c': 3}"),
    ("STRUCT(a VARCHAR, b VARCHAR)", "{'a': 'x', 'b': 'y'}", "{'a': 'x'', ''b'': ''y', 'b': NULL}"),
    ("MAP(VARCHAR, VARCHAR)", "map {'a=b': 'c'}", "map {'a': 'b=c'}"),
    ("MAP(VARCHAR, VARCHAR)", "map {'a': NULL}", "map {'a': 'NULL'}"),
    ("MAP(VARCHAR, VARCHAR)", "map {'a': 'x', 'b': 'y'}", "map {'b': 'y', 'a': 'x'}"),
    ("MAP(INTEGER, DOUBLE)", "map {1: 0.1}", "map {1: 0.1000000000000001}"),
    ("MAP(VARCHAR, BLOB)", "map {'k': '\\x00'::blob}", "map {'k': '\\x5Cx00'::blob}"),
    ("UNION(n INTEGER, s VARCHAR)", "union_value(n := 1)", "union_value(s := '1')"),
    ("UNION(s VARCHAR, t VARCHAR)", "union_value(s := 'x')", "union_value(t := 'x')"),
    ("UNION(a VARCHAR, b VARCHAR)", "union_value(a := NULL)", "union_value(b := NULL)"),
    ("UNION(n INTEGER, s VARCHAR)[]", "[union_value(n := 1)]", "[union_value(s := '1')]"),
    ("STRUCT(a VARCHAR)[]", "[{'a': 'x, y'}]", "[{'a': 'x'}, {'a': 'y'}]"),
    ("ENUM('a, b', 'a', 'b')[]", "['a, b']", "['a', 'b']"),
    ("GEOMETRY", "'POINT(1 2)'::geometry", "'POINT(1 3)'::geometry"),
]


def _pid(x: object) -> str:
    """A short test id."""
    return str(x)[:40]


@pytest.mark.parametrize(("typ", "a", "b"), FOUND_PAIRS, ids=_pid)
def test_two_different_values_are_found(tmp_path, typ, a, b):
    """Row 1 differs, rows 2 and 3 (a NULL) do not: exactly row 1 is reported."""
    if typ == "GEOMETRY" and DUCK_VERSION < (1, 5):
        pytest.skip("GEOMETRY is a core type from DuckDB 1.5")
    result = diff_pair(tmp_path, typ, a, b)
    assert found(result) == ONE
    assert not result.identical


#: Equal at the documented float scale (6) or by a documented normal form, so
#: they must compare equal - each with a planted change in another column of
#: row 2 to prove the comparison ran.
EQUAL_BY_RULE = [
    ("DOUBLE", "'-0.0'::double", "'0.0'::double"),  # one zero at any scale
    ("DOUBLE", "'nan'::double", "-('nan'::double)"),  # NaN payloads: one token
    ("DOUBLE", "4.9e-324", "0"),  # subnormal: zero at scale 6
    ("FLOAT", "'1.0000001'::float", "'1.0'::float"),
    ("DECIMAL(18,18)", "0.000000000000000001", "0.000000000000000002"),
    ("JSON", "'[1,2]'", "'[1, 2]'"),  # layout only
]


@pytest.mark.parametrize(("typ", "a", "b"), EQUAL_BY_RULE, ids=_pid)
def test_values_equal_by_the_rules_match_and_a_change_is_still_found(tmp_path, typ, a, b):
    """Rows 1 differ only below the rules' resolution; row 2 is really changed."""
    ddl = f"create table t (id int, v {typ}, w int)"
    pa = duck_file(tmp_path, "a", ddl, f"insert into t values (1, {a}, 1), (2, {a}, 2)")
    pb = duck_file(tmp_path, "b", ddl, f"insert into t values (1, {b}, 1), (2, {a}, 3)")
    result = run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t")
    assert found(result) == [(2, "different", ("w",))]


# Nested types fall back to DuckDB's varchar cast, and inherit every flaw the
# scalar rules were written to fix. The run warns "unmapped types, compared as
# raw text (may differ across engines for reasons other than the data)" - a
# warning about false alarms - and still says identical.


def _xf(reason: str):
    """A strict xfail mark."""
    return pytest.mark.xfail(strict=True, reason=reason)


NESTED_FALSE_MATCHES = [
    pytest.param(
        "VARCHAR[2]", "['a, b', 'c']", "['a', 'b, c']",
        marks=_xf("RT-HD5: a fixed-size ARRAY of VARCHAR prints its strings unquoted: ['a, b','c'] = ['a','b, c'], a false match"),
        id="array-varchar-comma",
    ),
    pytest.param(
        "VARCHAR[2]", "[NULL, 'x']", "['NULL', 'x']",
        marks=_xf("RT-HD5: a fixed-size ARRAY of VARCHAR prints NULL and 'NULL' alike, a false match"),
        id="array-varchar-null",
    ),
    pytest.param(
        "VARCHAR[2][]", "[['a, b', 'c']]", "[['a', 'b, c']]",
        marks=_xf("RT-HD5: a list of fixed-size VARCHAR arrays, the same unquoted text"),
        id="list-of-array-varchar",
    ),
    pytest.param(
        "VARCHAR[2][2]", "[['a, b', 'c'], ['d', 'e']]", "[['a', 'b, c'], ['d', 'e']]",
        marks=_xf("RT-HD5: a 2-D fixed-size VARCHAR array, the same unquoted text"),
        id="array-of-array-varchar",
    ),
    pytest.param(
        "STRUCT(a VARCHAR[2])", "{'a': ['x, y', 'z']}", "{'a': ['x', 'y, z']}",
        marks=_xf("RT-HD5: a fixed-size VARCHAR array inside a STRUCT, the same unquoted text"),
        id="struct-of-array-varchar",
    ),
    pytest.param(
        "STRUCT(a TIMETZ)", "{'a': '12:00:00+05:00:30'::timetz}", "{'a': '12:00:00+05:30'::timetz}",
        marks=_xf("RT-HD6: TIMETZ inside a STRUCT renders through the varchar cast, which drops the offset's seconds (COV-7, fixed only for a scalar TIMETZ)"),
        id="struct-timetz",
    ),
    pytest.param(
        "TIMETZ[2]", "['12:00:00+05:00:30'::timetz, '01:00:00+00'::timetz]", "['12:00:00+05:30'::timetz, '01:00:00+00'::timetz]",
        marks=_xf("RT-HD6: TIMETZ in a fixed-size ARRAY: the offset's seconds are dropped (COV-7)"),
        id="array-timetz",
    ),
    pytest.param(
        "MAP(INTEGER, TIMETZ)", "map {1: '12:00:00+05:00:30'::timetz}", "map {1: '12:00:00+05:30'::timetz}",
        marks=_xf("RT-HD6: TIMETZ as a MAP value: the offset's seconds are dropped (COV-7)"),
        id="map-timetz",
    ),
    pytest.param(
        "UNION(t TIMETZ, s VARCHAR)", "union_value(t := '12:00:00+05:00:30'::timetz)", "union_value(t := '12:00:00+05:30'::timetz)",
        marks=_xf("RT-HD6: TIMETZ as a UNION member (JSON of the varchar cast): the offset's seconds are dropped (COV-7)"),
        id="union-timetz",
    ),
    pytest.param(
        "STRUCT(u UNION(n INTEGER, s VARCHAR))", "{'u': union_value(n := 1)}", "{'u': union_value(s := '1')}",
        marks=_xf("RT-HD7: a UNION inside a STRUCT loses its tag in the varchar cast: 1::int = '1' (RT-A12 fixed only for a top-level UNION)"),
        id="struct-union-tag",
    ),
    pytest.param(
        "MAP(VARCHAR, UNION(n INTEGER, s VARCHAR))", "map {'k': union_value(n := 1)}", "map {'k': union_value(s := '1')}",
        marks=_xf("RT-HD7: a UNION as a MAP value loses its tag: 1::int = '1'"),
        id="map-union-tag",
    ),
]


@pytest.mark.parametrize(("typ", "a", "b"), NESTED_FALSE_MATCHES)
def test_nested_values_that_differ_are_found(tmp_path, typ, a, b):
    """DuckDB to DuckDB, same type both sides, so no cross-engine rendering
    excuse: the stored values differ and DuckDB's own `=` says so."""
    pa, pb = pair(tmp_path, typ, a, b)
    con = duckdb.connect(":memory:")
    try:
        assert con.execute(f"select ({a})::{typ} is distinct from ({b})::{typ}").fetchone() == (True,)
    finally:
        con.close()
    result = run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t")
    assert found(result) == ONE


@pytest.mark.sample_check_may_disagree
@pytest.mark.xfail(
    strict=True,
    reason="RT-HD8: an ENUM fixed-size ARRAY (`ENUM('a, b', ...)[2]`) is mapped to STRING "
    "(map_type cuts at '(' before it sees the [2]) and rendered as unquoted array text: "
    "['a, b','a'] = ['a','b, a'] in the walk; only a sampled row is caught",
)
def test_an_enum_array_difference_is_found_by_the_walk(tmp_path):
    """The sample check catches this row because the table has three rows; on
    a large table the row is outside the sample and the verdict is identical."""
    result = diff_pair(tmp_path, "ENUM('a, b', 'a', 'b', 'b, a')[2]", "['a, b', 'a']", "['a', 'b, a']")
    assert found(result) == ONE


@pytest.mark.sample_check_may_disagree
@pytest.mark.xfail(strict=True, reason="RT-HD8: as above, [NULL, 'x'] = ['NULL', 'x'] for an ENUM array")
def test_an_enum_array_null_is_found_by_the_walk(tmp_path):
    """A NULL element and the label 'NULL' print alike in array text."""
    result = diff_pair(tmp_path, "ENUM('x', 'NULL')[2]", "[NULL, 'x']", "['NULL', 'x']")
    assert found(result) == ONE


@pytest.mark.parametrize(
    "typ",
    [
        pytest.param("TIMESTAMP[2]", marks=_xf("RT-HD9: TIMESTAMP[2] maps to TIMESTAMP (prefix match) and strftime(array) fails: the diff crashes")),
        pytest.param("TIMESTAMPTZ[2]", marks=_xf("RT-HD9: TIMESTAMPTZ[2] maps to TIMESTAMP: the diff crashes")),
        pytest.param("TIMESTAMP_NS[2]", marks=_xf("RT-HD9: TIMESTAMP_NS[2] maps to TIMESTAMP: epoch_ns(array) fails, the diff crashes")),
        "DATE[2]",
    ],
)
def test_a_fixed_size_temporal_array_is_compared(tmp_path, typ):
    """A fixed-size array of a temporal type: a changed element is found."""
    result = diff_pair(tmp_path, typ, "['2020-01-01', '2020-01-02']", "['2020-01-01', '2020-01-03']")
    assert found(result) == ONE


@pytest.mark.parametrize("typ", ["DECIMAL(10,2)[2]", "DECIMAL(38,10)[2]"])
@pytest.mark.xfail(strict=True, reason="RT-HD9: DECIMAL(p,s)[n] maps to DECIMAL (cut at '(') and the cast of an array to decimal(38,6) fails: the diff crashes")
def test_a_fixed_size_decimal_array_is_compared(tmp_path, typ):
    """A fixed-size array of decimals: a changed element is found."""
    result = diff_pair(tmp_path, typ, "[1.10, 2]", "[1.20, 2]")
    assert found(result) == ONE
