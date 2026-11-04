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
@pytest.mark.parametrize("name", sorted(SHADOWS))
def test_a_macro_shadowing_a_builtin_cannot_hide_a_change(tmp_path, name):
    """Both files carry the same macro library (a team's shared helpers, or a
    hostile file); side B changed one row of 50,000. The tool must find it or
    refuse the file - never call the tables identical."""
    rows = "insert into t select i, 'v' || i from range(1, 50001) r(i)"
    ddl = "create table t (id int, v varchar)"
    pa = duck_file(tmp_path, "a", ddl, rows, SHADOWS[name])
    pb = duck_file(tmp_path, "b", ddl, rows, "update t set v = 'changed' where id = 4242", SHADOWS[name])
    with pytest.raises(ValueError, match=rf"\[side A: duckdb\] the database defines macro {name}, named like DuckDB built-ins"):
        open_duckdb(pa, "A")
    with pytest.raises(ValueError, match=rf"\[side B: duckdb\] the database defines macro {name}"):
        open_duckdb(pb, "B")


def test_a_user_type_named_json_cannot_hide_a_union_tag(tmp_path):
    """DuckDB refuses to shadow core types (below) but not `json`."""
    ddl = "create table t (id int, v union(n int, s varchar))"
    shadow = "create type json as varchar"
    pa = duck_file(tmp_path, "a", ddl, "insert into t values (1, union_value(n := 1)), (2, union_value(n := 2))", shadow)
    pb = duck_file(tmp_path, "b", ddl, "insert into t values (1, union_value(s := '1')), (2, union_value(n := 2))", shadow)
    with pytest.raises(ValueError, match=r"\[side A: duckdb\] the database defines type json"):
        open_duckdb(pa, "A")
    with pytest.raises(ValueError, match=r"\[side B: duckdb\] the database defines type json"):
        open_duckdb(pb, "B")


def test_a_macro_on_one_side_only_is_refused_and_the_other_side_compares(tmp_path):
    """The shadowing macro on one side only: that side is refused (RT-HD1);
    the honest file still compares, and finds its planted change."""
    rows = "insert into t select i, 'v' || i from range(1, 2001) r(i)"
    ddl = "create table t (id int, v varchar)"
    pa = duck_file(tmp_path, "a", ddl, rows)
    pb = duck_file(tmp_path, "b", ddl, rows, "update t set v = 'x' where id = 7", SHADOWS["md5"])
    with pytest.raises(ValueError, match=r"\[side B: duckdb\] the database defines macro md5"):
        open_duckdb(pb, "B")
    pc = duck_file(tmp_path, "c", ddl, rows, "update t set v = 'x' where id = 7")
    result = run_diff(open_duckdb(pa, "A"), open_duckdb(pc, "B"), "main.t", "main.t")
    assert found(result) == [(7, "different", ("v",))]


@pytest.mark.parametrize(
    "macro",
    [
        "create macro MD5(x) as 'z'",  # case-folded lookup
        "create macro strftime(a, b) as 'z'",  # a built-in the dialect uses that is not listed above
        "create macro coalesce_x(a) as a",  # not a built-in: allowed
    ],
)
def test_any_macro_named_like_a_builtin_is_refused(tmp_path, macro):
    """Defence in depth: the check is every built-in's name, not a list of
    the functions parity happens to call today."""
    p = duck_file(tmp_path, "m", "create table t (id int)", "insert into t values (1)", macro)
    if "coalesce_x" in macro:
        open_duckdb(p, "A").close()
        return
    with pytest.raises(ValueError, match=r"\[side A: duckdb\] the database defines macro"):
        open_duckdb(p, "A")


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


def test_a_sqlite_file_is_refused_without_loading_an_extension(tmp_path):
    """Verified 2026-11-04 with HOME pointed at an empty directory: the connect
    wrote `.duckdb/extensions/v1.5.5/windows_amd64/sqlite_scanner.duckdb_extension`
    there - downloaded during `get_dialect`. The dialect must refuse a file
    that is not a DuckDB database, naming the side, before any extension loads."""
    installed = list(
        (Path.home() / ".duckdb" / "extensions" / f"v{duckdb.__version__}").glob("*/sqlite_scanner.duckdb_extension")
    )
    if not installed and os.environ.get("PARITY_RTHD_ALLOW_NETWORK") != "1":
        # Without it installed, reproducing the bug *is* the download.
        pytest.skip("sqlite_scanner not installed; set PARITY_RTHD_ALLOW_NETWORK=1 to let the bug download it")
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


def test_a_sqlite_file_is_refused_before_duckdb_opens_it(tmp_path, monkeypatch):
    """RT-HD4 without the network: the header is read first, so duckdb.connect
    (which would install and load sqlite_scanner) is never called."""
    path = _sqlite_file(tmp_path)
    calls: list = []
    real = duckdb.connect

    def spy(database=":memory:", *a, **kw):

        """Record what duckdb.connect is asked to open."""
        calls.append(database)
        return real(database, *a, **kw)

    monkeypatch.setattr(duckdb, "connect", spy)
    with pytest.raises(ValueError, match=r"^\[side A: duckdb\] .*warehouse\.db is not a valid DuckDB database file \(it is a SQLite database\)"):
        get_dialect(f"duckdb:///{path}", side="A")
    assert calls == []


def test_a_schema_named_like_the_file_can_be_compared(tmp_path):
    """Written under another name, then renamed - as a file copied between
    machines is. main.t is identical, warehouse.t has row 9 changed."""
    ddl = "create table {s}.t (id int, v varchar)"
    rows = "insert into {s}.t select i, 'v' || i from range(1, 101) r(i)"
    src = duck_file(
        tmp_path, "src", "create schema warehouse",
        ddl.format(s="main"), rows.format(s="main"),
        ddl.format(s="warehouse"), rows.format(s="warehouse"),
        "update warehouse.t set v = 'changed' where id = 9",
    )
    target = tmp_path / "warehouse.duckdb"
    target.write_bytes(Path(src).read_bytes())
    ref = duck_file(tmp_path, "ref", ddl.format(s="main"), rows.format(s="main"))
    result = run_diff(open_duckdb(ref, "A"), open_duckdb(str(target), "B"), "main.t", "warehouse.t")
    assert found(result) == [(9, "different", ("v",))]


def test_a_file_named_like_a_builtin_catalog_still_reads_its_own_tables(tmp_path):
    """temp.duckdb, system.duckdb, main.duckdb, memory.duckdb, pg_catalog.duckdb:
    DuckDB renames the catalog, and the right table is read."""
    ref = duck_file(tmp_path, "ref", "create table t (id int, v varchar)",
                    "insert into t select i, 'v' || i from range(1, 101) r(i)")
    for stem in ("temp", "system", "main", "memory", "pg_catalog"):
        p = duck_file(
            tmp_path, stem, "create schema x",
            "create table main.t (id int, v varchar)",
            "insert into main.t select i, 'v' || i from range(1, 101) r(i)",
            "create table x.t as select * from main.t",
            "update x.t set v = 'changed' where id = 9",
        )
        assert found(run_diff(open_duckdb(ref, "A"), open_duckdb(p, "B"), "main.t", "x.t")) == [
            (9, "different", ("v",))
        ], stem
        assert found(run_diff(open_duckdb(ref, "A"), open_duckdb(p, "B"), "main.t", "main.t")) == [], stem


def test_a_file_named_information_schema_can_be_compared(tmp_path):
    """The column lookup must name the system catalog's information_schema."""
    ddl = "create table t (id int, v varchar)"
    rows = "insert into t select i, 'v' || i from range(1, 101) r(i)"
    src = duck_file(tmp_path, "src", ddl, rows, "update t set v = 'x' where id = 3")
    target = tmp_path / "information_schema.duckdb"
    target.write_bytes(Path(src).read_bytes())
    ref = duck_file(tmp_path, "ref", ddl, rows)
    result = run_diff(open_duckdb(ref, "A"), open_duckdb(str(target), "B"), "main.t", "main.t")
    assert found(result) == [(3, "different", ("v",))]


def _view_diff(pa: str, pb: str):
    """`main.vw` compared, or None where DuckDB itself refuses to bind it:
    1.3 and 1.4 raise "Contents of view were altered" - loudly, naming the
    side - for a view whose base table changed under it (RT-HD13 is 1.5+)."""
    if DUCK_VERSION < (1, 5):
        with pytest.raises(ValueError, match=r"\[side [AB]: duckdb\].*(altered|bind)"):
            run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.vw", "main.vw")
        return None
    return run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.vw", "main.vw")


def test_a_view_over_an_altered_column_compares_its_real_type(tmp_path):
    """`select * from vw` returns VARCHAR; the catalog says INTEGER."""
    def mk(value: str) -> tuple[str, ...]:
        """The statements building one side."""
        return (
            "create table t (id int, v int)",
            "create view vw as select * from t",
            "alter table t alter v type varchar",
            f"insert into t values (1, {value}), (2, 'x')",
        )

    pa = duck_file(tmp_path, "a", *mk("'\\N'"))
    pb = duck_file(tmp_path, "b", *mk("NULL"))
    # The base table is right ...
    assert found(run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t")) == ONE
    # ... the view over it must be too.
    result = _view_diff(pa, pb)
    if result is not None:
        assert found(result) == ONE


@pytest.mark.parametrize(
    ("old", "new", "a", "b"),
    [
        ("date", "timestamp", "'2020-01-01 00:00:01'", "'2020-01-01 00:00:02'"),  # time of day dropped
        ("timestamp", "timestamp_ns", "'2020-01-01 00:00:00.000000001'", "'2020-01-01 00:00:00.000000002'"),
        ("int", "union(n int, s varchar)", "union_value(n := 1)", "union_value(s := '1')"),  # tag dropped
    ],
    ids=["date-to-timestamp", "timestamp-to-ns", "int-to-union"],
)
def test_a_view_over_a_retyped_column_finds_the_change(tmp_path, old, new, a, b):
    """The base table finds row 1; the `select *` view over it must too."""
    def mk(v: str) -> tuple[str, ...]:
        """The statements building one side."""
        return (
            f"create table t (id int, v {old})",
            "create view vw as select * from t",
            f"alter table t alter v type {new} using NULL",
            f"insert into t values (1, {v}), (2, NULL)",
        )

    pa = duck_file(tmp_path, "a", *mk(a))
    pb = duck_file(tmp_path, "b", *mk(b))
    assert found(run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t")) == ONE
    result = _view_diff(pa, pb)
    if result is not None:
        assert found(result) == ONE


def test_a_view_that_gained_a_column_compares_it(tmp_path):
    """vw returns (id, v, w); w differs; the catalog lists (id, v)."""
    def mk(w: str) -> tuple[str, ...]:
        """The statements building one side."""
        return (
            "create table t (id int, v int)",
            "create view vw as select * from t",
            "alter table t add column w varchar",
            f"insert into t values (1, 1, '{w}'), (2, 2, 'same')",
        )

    pa = duck_file(tmp_path, "a", *mk("a"))
    pb = duck_file(tmp_path, "b", *mk("b"))
    result = _view_diff(pa, pb)
    if result is not None:
        assert found(result) == [(1, "different", ("w",))]
        assert any("view main.vw: the catalog lists" in w for w in result.warnings)


def test_a_view_needing_an_extension_fails_without_installing_it(tmp_path):
    """A view in the file calls a spatial function. Read with HOME pointed at an
    empty directory (no extensions installed), the side must fail loudly and
    nothing may be downloaded into that directory."""
    path = str(tmp_path / "ext.duckdb")
    con = duckdb.connect(path)
    try:
        con.execute("load spatial")
    except duckdb.Error:
        con.close()
        pytest.skip("the spatial extension is not installed here (installing it needs the network)")
    try:
        con.execute("create table t (id int, x double)")
        con.execute("insert into t values (1, 1.0)")
        con.execute("create view v as select id, st_astext(st_point(x, x)) as g from t")
    finally:
        con.close()
    home = tmp_path / "home"
    home.mkdir()
    code = (
        "import sys\nfrom parity.dialects.base import get_dialect\nfrom parity.engine import diff\n"
        "a = get_dialect('duckdb:///' + sys.argv[1], side='A')\nb = get_dialect('duckdb:///' + sys.argv[1], side='B')\n"
        "try:\n    diff(a, b, 'main.v', 'main.v', key='id')\nexcept ValueError as e:\n    print('REFUSED', str(e)[:200])\n"
        "else:\n    print('RAN')\n"
    )
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    out = subprocess.run([sys.executable, "-c", code, path], env=env, capture_output=True, text=True, timeout=300)
    assert "REFUSED [side A" in out.stdout, out.stdout + out.stderr
    assert not [p for p in home.rglob("*") if p.is_file()], "an extension was installed"


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
        assert "already open" in str(exc.value) or "held.duckdb" in str(exc.value)
    finally:
        writer.kill()
        writer.wait()


def test_a_read_only_file_is_compared(tmp_path):
    """A file the user may not write (read-only attribute): opened and
    compared, row 9 found, file unchanged."""
    import stat

    pa = duck_file(tmp_path, "a", "create table t (id int, v int)", "insert into t select i, i from range(1, 101) r(i)")
    pb = duck_file(tmp_path, "b", "create table t (id int, v int)", "insert into t select i, i from range(1, 101) r(i)", "update t set v = 0 where id = 9")
    before = Path(pa).read_bytes()
    os.chmod(pa, stat.S_IREAD)
    try:
        result = run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t")
    finally:
        os.chmod(pa, stat.S_IREAD | stat.S_IWRITE)
    assert found(result) == [(9, "different", ("v",))]
    assert Path(pa).read_bytes() == before


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
    pytest.param("VARCHAR[]", "NULL", "[]", id="list-empty-vs-null"),  # RT-HD11 on 1.3-1.4
    pytest.param("INTEGER[]", "[]", "NULL", id="int-list-empty-vs-null"),  # RT-HD11 on 1.3-1.4
    ("VARCHAR[]", "['\\,']", "[',']"),
    ("VARCHAR[]", "['a\\,', 'b']", "['a', '\\,b']"),
    ("INTEGER[][]", "[[1, 2], [3]]", "[[1], [2, 3]]"),
    ("VARCHAR[][]", "[['a'], ['b']]", "[['a', 'b']]"),
    ("VARCHAR[][]", "[[NULL]]", "[['NULL']]"),
    ("VARCHAR[][2]", "[['a, b'], ['c']]", "[['a', 'b'], ['c']]"),
    ("INTEGER[2]", "[1, 2]", "[1, 3]"),
    ("DOUBLE[2]", "[0.1, 2]", "[0.100001, 2]"),
    ("FLOAT[3]", "[1.0, 2.0, 3.0]", "[1.5, 2.0, 3.0]"),
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
    ("DOUBLE[]", "['nan'::double]", "['inf'::double]"),
    ("DOUBLE[]", "['nan'::double]", "[NULL]"),
    ("FLOAT[]", "['nan'::float]", "[NULL]"),
    ("TIMESTAMP_S[]", "['0001-01-01 00:00:00']", "['0001-01-01 00:00:01']"),
    ("TIMESTAMP_MS[]", "['9999-12-31 23:59:59.999']", "['9999-12-31 23:59:59.998']"),
    ("TIMESTAMPTZ[]", "['infinity']", "['-infinity']"),
    ("DATE[]", "['0001-01-01 (BC)']", "['0001-01-01']"),
    ("DECIMAL(38,30)[]", "[1.000001]", "[1.000002]"),
    ("VARCHAR[]", "[chr(0)]", "['']"),
    ("VARCHAR[]", "['\\N']", "[NULL]"),
    ("BLOB[]", "[NULL]", "['\\x5CN'::blob]"),
    ("INTERVAL[]", "[interval '1 day']", "[interval '24 hours']"),
    ("BOOLEAN[]", "[true, false]", "[false, true]"),
    ("ENUM('a','b')[]", "[NULL]", "['a']"),
    ("BIT[]", "[NULL]", "['0'::bit]"),
]


def _pid(x: object) -> str:
    """A short test id."""
    return str(x)[:40]


@pytest.mark.parametrize(("typ", "a", "b"), FOUND_PAIRS, ids=_pid)
def test_two_different_values_are_found(tmp_path, typ, a, b):
    """Row 1 differs, rows 2 and 3 (a NULL) do not: exactly row 1 is reported."""
    if typ == "GEOMETRY" and DUCK_VERSION < (1, 5):
        pytest.skip("GEOMETRY is a core type from DuckDB 1.5")
    if typ == "TIME_NS" and DUCK_VERSION < (1, 5):
        pytest.skip("TIME_NS is a type from DuckDB 1.5")
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
    # A fixed-size ARRAY is a list, its elements at the float scale (RT-HD5).
    ("DOUBLE[2]", "[0.1, 2]", "[0.1000000000000001, 2]"),
    ("FLOAT[3]", "[1.0, 2.0, 3.0]", "[1.0000001, 2.0, 3.0]"),
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


def _xf_pre2(reason: str):
    """A strict xfail before DuckDB 2.0, whose varchar cast quotes array
    strings and keeps a TIMETZ offset's seconds (measured: these pass there)."""
    return pytest.mark.xfail(DUCK_VERSION < (2, 0), strict=True, reason=reason + " (DuckDB < 2.0)")


#: RT-HD14 (fixed): on DuckDB 2.0 a column's collation reached the key-
#: uniqueness count; the keys below hold collation-equal texts.


NESTED_FALSE_MATCHES = [
    pytest.param(
        "VARCHAR[2]", "['a, b', 'c']", "['a', 'b, c']",
        id="array-varchar-comma",
    ),
    pytest.param(
        "VARCHAR[2]", "[NULL, 'x']", "['NULL', 'x']",
        id="array-varchar-null",
    ),
    pytest.param(
        "VARCHAR[2][]", "[['a, b', 'c']]", "[['a', 'b, c']]",
        id="list-of-array-varchar",
    ),
    pytest.param(
        "VARCHAR[2][2]", "[['a, b', 'c'], ['d', 'e']]", "[['a', 'b, c'], ['d', 'e']]",
        id="array-of-array-varchar",
    ),
    pytest.param(
        "STRUCT(a VARCHAR[2])", "{'a': ['x, y', 'z']}", "{'a': ['x', 'y, z']}",
        id="struct-of-array-varchar",
    ),
    pytest.param(
        "STRUCT(a TIMETZ)", "{'a': '12:00:00+05:00:30'::timetz}", "{'a': '12:00:00+05:30'::timetz}",
        id="struct-timetz",
    ),
    pytest.param(
        "TIMETZ[2]", "['12:00:00+05:00:30'::timetz, '01:00:00+00'::timetz]", "['12:00:00+05:30'::timetz, '01:00:00+00'::timetz]",
        id="array-timetz",
    ),
    pytest.param(
        "MAP(INTEGER, TIMETZ)", "map {1: '12:00:00+05:00:30'::timetz}", "map {1: '12:00:00+05:30'::timetz}",
        id="map-timetz",
    ),
    pytest.param(
        "UNION(t TIMETZ, s VARCHAR)", "union_value(t := '12:00:00+05:00:30'::timetz)", "union_value(t := '12:00:00+05:30'::timetz)",
        id="union-timetz",
    ),
    pytest.param(
        "STRUCT(u UNION(n INTEGER, s VARCHAR))", "{'u': union_value(n := 1)}", "{'u': union_value(s := '1')}",
        id="struct-union-tag",
    ),
    pytest.param(
        "MAP(VARCHAR, UNION(n INTEGER, s VARCHAR))", "map {'k': union_value(n := 1)}", "map {'k': union_value(s := '1')}",
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


def _variant_file(tmp_path: Path, name: str, *statements: str) -> str:
    """A DuckDB file at storage version v1.5.0, which VARIANT needs."""
    path = str(tmp_path / f"{name}.duckdb")
    con = duckdb.connect()
    try:
        con.execute(f"attach '{path}' as db (storage_version 'v1.5.0')")
        con.execute("use db")
        for sql in statements:
            con.execute(sql)
    finally:
        con.close()
    return path


VARIANT_PAIRS = [
    pytest.param("1::variant", "'1'::variant", id="int-vs-text"),
    pytest.param("[1]::variant", "['1']::variant", id="list-int-vs-text"),
    pytest.param("{'a':1}::variant", "{'a':'1'}::variant", id="struct"),
    pytest.param("1::int::variant", "1::bigint::variant", id="int32-vs-int64"),
    pytest.param("'2020-01-01 00:00:00.000000001'::timestamp_ns::variant", "'2020-01-01 00:00:00.000000002'::timestamp_ns::variant", id="ns"),
    pytest.param("interval '1 month'::variant", "interval '30 days'::variant", id="interval"),
    pytest.param("[NULL]::varchar[]::variant", "['NULL']::variant", id="null-vs-NULL"),
    pytest.param("'\\x00'::blob::variant", "'\\x5Cx00'::blob::variant", id="blob"),
    pytest.param(
        "'12:00:00+05:00:30'::timetz::variant", "'12:00:00+05:30'::timetz::variant",
        id="timetz-offset-seconds",
    ),
]


@pytest.mark.skipif(DUCK_VERSION < (1, 5), reason="VARIANT is a type from DuckDB 1.5")
@pytest.mark.parametrize(("a", "b"), VARIANT_PAIRS)
def test_variant_values_that_differ_are_found(tmp_path, a, b):
    """VARIANT keeps its type tag and its value; row 1 differs."""
    ddl = "create table t (id int, v variant)"
    pa = _variant_file(tmp_path, "va", ddl, f"insert into t values (1, {a}), (2, {a}), (3, NULL)")
    pb = _variant_file(tmp_path, "vb", ddl, f"insert into t values (1, {b}), (2, {a}), (3, NULL)")
    result = run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t")
    assert found(result) == ONE


def test_an_enum_array_difference_is_found_by_the_walk(tmp_path):
    """The sample check catches this row because the table has three rows; on
    a large table the row is outside the sample and the verdict is identical."""
    result = diff_pair(tmp_path, "ENUM('a, b', 'a', 'b', 'b, a')[2]", "['a, b', 'a']", "['a', 'b, a']")
    assert found(result) == ONE


def test_an_enum_array_null_is_found_by_the_walk(tmp_path):
    """A NULL element and the label 'NULL' print alike in array text."""
    result = diff_pair(tmp_path, "ENUM('x', 'NULL')[2]", "[NULL, 'x']", "['NULL', 'x']")
    assert found(result) == ONE


@pytest.mark.parametrize(
    "typ",
    [
        "TIMESTAMP[2]",  # RT-HD9: these crashed, mapped to their scalar type
        "TIMESTAMPTZ[2]",
        "DATE[2]",
    ],
)
def test_a_fixed_size_temporal_array_is_compared(tmp_path, typ):
    """A fixed-size array of a temporal type: a changed element is found."""
    result = diff_pair(tmp_path, typ, "['2020-01-01', '2020-01-02']", "['2020-01-01', '2020-01-03']")
    assert found(result) == ONE


@pytest.mark.parametrize("typ", ["TIMESTAMP_NS[2]", "TIMESTAMP_NS[]"])
def test_a_nanosecond_array_is_refused_like_a_nanosecond_list(tmp_path, typ):
    """RT-HD9: TIMESTAMP_NS[2] crashed (epoch_ns of an array). A list cannot
    carry the scalar column's nanosecond caveat, so both are refused, naming
    the column - never compared with the digits silently dropped."""
    with pytest.raises(ValueError, match=r"list column 'v' holds TIMESTAMP_NS elements"):
        diff_pair(tmp_path, typ, "['2020-01-01', '2020-01-02']", "['2020-01-01', '2020-01-03']")


@pytest.mark.parametrize("typ", ["DECIMAL(10,2)[2]", "DECIMAL(38,10)[2]"])
def test_a_fixed_size_decimal_array_is_compared(tmp_path, typ):
    """A fixed-size array of decimals: a changed element is found."""
    result = diff_pair(tmp_path, typ, "[1.10, 2]", "[1.20, 2]")
    assert found(result) == ONE


@pytest.mark.parametrize(
    ("schema", "table"),
    [("main", 'we"ird tab'), ("my schema", "Ünï 表"), ("main", "select"), ("SELECT", "FROM"), ("main", "ORDERS"), ("main", " lead")],
)
def test_odd_schema_and_table_names(tmp_path, schema, table):
    """Quotes, spaces, Unicode, reserved words, upper case, a leading space;
    the key matched case-insensitively (`--key id` for column "Id")."""
    qs = '"' + schema.replace('"', '""') + '"'
    qt = '"' + table.replace('"', '""') + '"'
    st = (
        f"create schema if not exists {qs}",
        f'create table {qs}.{qt} ("Id" int, "v a" varchar)',
        f"insert into {qs}.{qt} select i, 'v' || i from range(1, 11) r(i)",
    )
    pa = duck_file(tmp_path, "a", *st)
    pb = duck_file(tmp_path, "b", *st, f"update {qs}.{qt} set \"v a\" = 'x' where \"Id\" = 3")
    name = f"{schema}.{table}"
    result = run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), name, name, key="id")
    assert found(result) == [(3, "different", ("v a",))]


def test_a_table_name_with_a_dot_is_refused_clearly(tmp_path):
    """DuckDB allows `"a.b"`; the tool splits on dots and refuses, naming
    the side - a limitation, never a different table read."""
    p = duck_file(tmp_path, "a", 'create table "a.b" (id int)', 'insert into "a.b" values (1)')
    with pytest.raises(ValueError, match=r"\[side A: duckdb\].*3 dot-separated parts"):
        run_diff(open_duckdb(p, "A"), open_duckdb(p, "B"), "main.a.b", "main.a.b")


def test_a_writer_cannot_change_the_file_during_a_diff(tmp_path):
    """The read-only side holds the file: a writer process started mid-run
    cannot open it, so the walk sees one version (DuckDB's snapshot)."""
    p = duck_file(tmp_path, "a", "create table t (id int, v int)", "insert into t select i, i from range(1, 101) r(i)")
    q = duck_file(tmp_path, "b", "create table t (id int, v int)", "insert into t select i, i from range(1, 101) r(i)", "update t set v = 0 where id = 50")
    a, b = open_duckdb(p, "A"), open_duckdb(q, "B")
    try:
        code = "import duckdb, sys\ncon = duckdb.connect(sys.argv[1])\ncon.execute('delete from t')\nprint('WROTE')\n"
        out = subprocess.run([sys.executable, "-c", code, p], capture_output=True, text=True, timeout=120)
        assert "WROTE" not in out.stdout
        from parity.engine import diff
        result = diff(a, b, "main.t", "main.t", key="id")
    finally:
        a.close()
        b.close()
    assert found(result) == [(50, "different", ("v",))]


def test_a_700_column_table_with_odd_names_and_5mb_rows(tmp_path):
    """700 columns named with spaces, quotes and Unicode, plus a 5 MB string:
    a change in the last column and one in the last byte of the big string."""
    n = 700
    cols = ", ".join(f'"c {i} ""q"" ü" varchar' for i in range(n))
    ins = ", ".join(f"'v{i}_' || i" for i in range(n))
    mk = (
        f"create table t (id int, {cols}, big varchar)",
        f"insert into t select i, {ins}, repeat('x', 5000000) from range(1, 21) r(i)",
    )
    pa = duck_file(tmp_path, "a", *mk)
    pb = duck_file(
        tmp_path, "b", *mk,
        f"update t set \"c {n - 1} \"\"q\"\" ü\" = 'changed' where id = 7",
        "update t set big = repeat('x', 4999999) || 'y' where id = 9",
    )
    result = run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t")
    assert found(result) == [(7, "different", (f'c {n - 1} "q" ü',)), (9, "different", ("big",))]


# ======================================================================= keys

#: (type, k1, k2, k3, k4): side A holds k1..k3, side B changes k2's value,
#: deletes k3 and adds k4. Keys that differ only by case, accent,
#: normalisation, a sub-microsecond digit, an offset second, ... stay apart.
KEYS = [
    ("UHUGEINT", "0", "170141183460469231731687303715884105728", "340282366920938463463374607431768211455", "340282366920938463463374607431768211454"),
    ("HUGEINT", "-170141183460469231731687303715884105728", "0", "170141183460469231731687303715884105727", "-1"),
    ("UBIGINT", "0", "9223372036854775808", "18446744073709551615", "18446744073709551614"),
    ("BIGINT", "-9223372036854775808", "0", "9223372036854775807", "9223372036854775806"),
    ("UTINYINT", "0", "128", "255", "254"),
    ("TINYINT", "-128", "0", "127", "126"),
    ("DECIMAL(38,0)", "-99999999999999999999999999999999999999", "0", "99999999999999999999999999999999999999", "1"),
    ("DECIMAL(10,5)", "1.5", "1.50001", "-0.00001", "2"),
    ("DECIMAL(38,37)", "0.0000000000000000000000000000000000001", "0.0000000000000000000000000000000000002", "-9.9999999999999999999999999999999999999", "0"),
    ("UUID", "'00000000-0000-0000-0000-000000000000'", "'ffffffff-ffff-ffff-ffff-ffffffffffff'", "'7fffffff-ffff-ffff-ffff-ffffffffffff'", "'80000000-0000-0000-0000-000000000000'"),
    ("BLOB", "''::blob", "'\\x00'::blob", "'\\x00\\x00'::blob", "'\\xff'::blob"),
    ("VARCHAR", "''", "chr(31)", "'\\N'", "'\\'"),
    ("VARCHAR COLLATE NOCASE", "'a'", "'A'", "'b'", "'B'"),
    ("VARCHAR COLLATE NOACCENT", "'e'", "'é'", "'E'", "'è'"),
    ("VARCHAR COLLATE NFC", "'é'", "'e' || chr(769)", "'x'", "'y'"),
    ("VARCHAR COLLATE de", "'ss'", "'ß'", "'a'", "'b'"),
    ("VARCHAR COLLATE en", "'a'", "'A'", "'ä'", "'b'"),
    ("TIMESTAMP_NS", "'2020-01-01 00:00:00.000000001'", "'2020-01-01 00:00:00.000000002'", "'1677-09-22 00:00:00'", "'2262-01-01 00:00:00'"),
    ("TIMESTAMP", "'infinity'", "'-infinity'", "'2020-01-01'", "'0001-01-01 (BC)'"),
    ("TIMESTAMPTZ", "'2020-01-01 00:00:00+00'", "'2020-01-01 00:00:00.000001+00'", "'infinity'", "'-infinity'"),
    ("TIMESTAMP_S", "'0001-01-01'", "'9999-12-31 23:59:59'", "'1970-01-01'", "'1969-12-31 23:59:59'"),
    ("TIMESTAMP_MS", "'0001-01-01'", "'9999-12-31 23:59:59.999'", "'1970-01-01'", "'1969-12-31 23:59:59.999'"),
    ("DATE", "'infinity'", "'-infinity'", "'0001-01-01 (BC)'", "'5881580-07-10'"),
    ("TIME", "'00:00:00'", "'24:00:00'", "'23:59:59.999999'", "'12:00:00'"),
    ("TIMETZ", "'12:00:00+05:00:30'", "'12:00:00+05:30'", "'12:00:00-15:59'", "'12:00:00+15:59'"),
    ("INTERVAL", "interval '1 month'", "interval '30 days'", "interval '720 hours'", "interval '-1 month'"),
    ("BIT", "'0'::bit", "'00'::bit", "'1'::bit", "'01'::bit"),
    ("ENUM('a', 'b', 'c', 'd')", "'a'", "'b'", "'c'", "'d'"),
    ("JSON", "'1'", "'1.0'", "'\"1\"'", "'[1]'"),
    ("STRUCT(a INTEGER)", "{'a': 1}", "{'a': 2}", "{'a': 3}", "{'a': 4}"),
]


@pytest.mark.parametrize(("typ", "k1", "k2", "k3", "k4"), KEYS, ids=lambda x: str(x)[:24])
def test_every_key_type_keeps_distinct_keys_apart(tmp_path, typ, k1, k2, k3, k4):
    """A change, a deletion and an insertion, each under its own key."""
    ddl = f"create table t (id {typ}, v int)"
    pa = duck_file(tmp_path, "ka", ddl, f"insert into t values ({k1}, 1), ({k2}, 2), ({k3}, 3)")
    pb = duck_file(tmp_path, "kb", ddl, f"insert into t values ({k1}, 1), ({k2}, 20), ({k4}, 4)")
    result = run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t")
    kinds = sorted(d.kind for d in result.diffs)
    assert kinds == ["different", "only_in_a", "only_in_b"]
    assert not result.identical


@pytest.mark.parametrize("coll", ["nocase", "noaccent", "nfc", "de", "en_us", "de.noaccent", "nocase.noaccent.nfc"])
def test_keys_equal_under_a_collation_stay_distinct(tmp_path, coll):
    """'a'/'A', 'é' composed/decomposed, 'é'/'e', 'ss'/'ß', 'a'/'a ' are
    different keys whatever the column's collation: one-sided on each side,
    and four such keys on both sides with one changed row."""
    ddl = f"create table t (k varchar collate {coll}, v int)"
    for ka, kb in [("'a'", "'A'"), ("'é'", "'e' || chr(769)"), ("'é'", "'e'"), ("'ss'", "'ß'"), ("'a'", "'a '")]:
        pa = duck_file(tmp_path, "a", ddl, f"insert into t values ({ka}, 1), ('z', 2)")
        pb = duck_file(tmp_path, "b", ddl, f"insert into t values ({kb}, 1), ('z', 3)")
        r = run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t", key="k")
        assert sorted(d.kind for d in r.diffs) == ["different", "only_in_a", "only_in_b"], (ka, kb)
    rows = "insert into t values ('a', 1), ('A', {}), ('é', 3), ('e' || chr(769), 4)"
    pa = duck_file(tmp_path, "a", ddl, rows.format(2))
    pb = duck_file(tmp_path, "b", ddl, rows.format(20))
    r = run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t", key="k")
    assert found(r) == [("A", "different", ("v",))]


def test_a_varchar_array_key_is_refused_as_a_list_key(tmp_path):
    """RT-HD5 as a key: a fixed-size ARRAY is a list (its elements by the
    value rules, which round floats), and a list key is refused on every
    version - before the fix ['a, b','c'] and ['a','b, c'] shared one key
    text on DuckDB < 2.0."""
    ddl = "create table t (id VARCHAR[2], v int)"
    rows = "insert into t values (['a, b', 'c'], 1), (['a', 'b, c'], 2)"
    pa = duck_file(tmp_path, "a", ddl, rows)
    pb = duck_file(tmp_path, "b", ddl, rows, "update t set v = 3 where id = ['a', 'b, c']")
    with pytest.raises(ValueError, match="list column 'id' cannot be used as a key"):
        run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t")


# ===================================================================== --where


def test_a_list_literal_cannot_smuggle_a_parenthesis_past_the_filter_check():
    """DuckDB parses the predicate as `(v = 'x]') or (v = '[' and <bounds>)`.
    The walk's count checks then report the range unexplained and the escaped
    row twice (verified), so no false match follows - but the check's promise,
    that a predicate cannot close the parentheses it is wrapped in, is broken."""
    assert row_filter_problem("v = ['x]'][1]) or (v = ['['][1]") is not None


@pytest.mark.parametrize(
    "pred",
    [
        "v = ['a]'][1]) or (1 = 1 and v = ['['][1]",
        "list_contains(['x]'], v)) or (list_contains(['['], v)",
    ],
)
def test_more_list_literals_cannot_smuggle_a_parenthesis(pred):
    """DuckDB reads each `]` inside a quoted string as text: both predicates
    close the parenthesis they are wrapped in (the second matched a row under
    `(<pred>) and false`, measured on 1.5.5), so the check must refuse them."""
    assert row_filter_problem(pred) is not None


def test_the_escaped_filter_is_caught_by_the_walk_s_count_checks(tmp_path, monkeypatch):
    """Defence in depth for RT-HD2: the scanner now refuses this predicate, so
    the guard is switched off here to check the second layer - the walk's count
    checks still make an escaped filter unexplained, never identical."""
    from parity.dialects.base import Dialect

    monkeypatch.setattr(Dialect, "check_where", lambda self, where: None)
    assert row_filter_problem("v = ['x]'][1]) or (w >= ['['][1]") is not None
    pred = "v = ['x]'][1]) or (w >= ['['][1]"
    mk = (
        "create table t (id int, v varchar, w varchar)",
        "insert into t select i, 'v' || i, 'w' || i from range(1, 2001) r(i)",
        "insert into t values (5000, 'x]', 'a')",
    )
    pa = duck_file(tmp_path, "a", *mk)
    pb = duck_file(tmp_path, "b", *mk, "update t set w = 'changed' where id = 77")
    result = run_diff(
        open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t", where_a=pred, where_b=pred
    )
    assert not result.identical
    assert (77, "different", ("w",)) in found(result)
    assert result.unexplained


@pytest.mark.parametrize(
    "pred",
    [
        "list_contains(l, 77) or list_contains(l, 15001)",
        "s.a = 77 or s['a'] = 15000",
        "len([x for x in l if x > 0]) = 2 and id in (77, 15000)",
        "id = any([77, 15000])",
        "struct_pack(a := id).a in (77, 15000)",
        "e'\\x41' = 'A' and id in (77, 15000)",
        "\"id\" in (77, 15000)",
        "COLUMNS('id') in (77, 15000)",
        "regexp_matches(v, '^(v77|v15000|changed)$')",
        "v similar to 'v(77|15000)' or v = 'changed'",
    ],
)
def test_duckdb_specific_predicates_filter_both_sides(tmp_path, pred):
    """Lists, structs, lambdas, `E''` strings, COLUMNS(), regexes: the two
    planted changes inside the filter are found, nothing else."""
    ddl = "create table t (id int, v varchar, l int[], s struct(a int))"
    rows = "insert into t select i, 'v' || i, [i, i + 1], {'a': i} from range(1, 20001) r(i)"
    pa = duck_file(tmp_path, "a", ddl, rows)
    pb = duck_file(tmp_path, "b", ddl, rows, "update t set v = 'changed' where id in (77, 15000)")
    result = run_diff(
        open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t", where_a=pred, where_b=pred
    )
    assert [k for k, _, _ in found(result)] == [77, 15000]


@pytest.mark.parametrize("pred", ["random() < 0.5", "random() < 0.99"])
def test_a_nondeterministic_filter_is_never_identical(tmp_path, pred):
    """Every query sees a different row set: never a clean verdict. (A
    `using sample` subquery is deterministic on DuckDB 2.0 for a small table,
    so it is not used here.)"""
    ddl = "create table t (id int, v varchar)"
    rows = "insert into t select i, 'v' || i from range(1, 5001) r(i)"
    pa = duck_file(tmp_path, "a", ddl, rows)
    pb = duck_file(tmp_path, "b", ddl, rows, "update t set v = 'x' where id = 2500")
    result = run_diff(
        open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t",
        where_a=pred, where_b=pred, verify_sample=0,
    )
    assert not result.identical


@pytest.mark.parametrize("pred", ["id = $1", "id = ?"])
def test_a_parameter_placeholder_in_a_filter_is_a_clear_error(tmp_path, pred):
    """No parameters are bound: an error naming the side, not a crash."""
    p = duck_file(tmp_path, "a", "create table t (id int)", "insert into t values (1)")
    with pytest.raises(ValueError, match=r"\[side A: duckdb\]"):
        run_diff(open_duckdb(p, "A"), open_duckdb(p, "B"), "main.t", "main.t", where_a=pred)


# ======================================================================= --map


def _map_files(tmp_path: Path) -> tuple[str, str]:
    """Side B swapped columns a and b, upper-cased the key and changed the
    case of two quoted columns; rows 3, 4 and 6 really differ."""
    pa = duck_file(
        tmp_path, "ma",
        'create table t (id int, a varchar, b varchar, "Mixed Case" int, "we""ird" int)',
        "insert into t select i, 'a' || i, 'b' || i, i, i from range(1, 101) r(i)",
    )
    pb = duck_file(
        tmp_path, "mb",
        'create table t ("ID" int, b varchar, a varchar, "mixed case" int, "WE""IRD" int)',
        "insert into t select i, 'a' || i, 'b' || i, i, i from range(1, 101) r(i)",
        "update t set b = 'x' where \"ID\" = 3",
        "update t set \"mixed case\" = 0 where \"ID\" = 4",
        "update t set \"WE\"\"IRD\" = 0 where \"ID\" = 6",
    )
    return pa, pb


@pytest.mark.parametrize(
    "column_map",
    [{"a": "b", "b": "a"}, {"a": "B", "b": "A"}, {"id": "ID", "a": "b", "b": "a"}, {"Mixed Case": "MIXED CASE", "a": "b", "b": "a"}],
)
def test_map_swaps_columns_with_duckdb_quoting(tmp_path, column_map):
    """A swap, case-insensitive names, the key itself, quoted names: the
    three planted changes, reported under side A's names."""
    pa, pb = _map_files(tmp_path)
    result = run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t", column_map=column_map)
    assert found(result) == [(3, "different", ("a",)), (4, "different", ("Mixed Case",)), (6, "different", ('we"ird',))]


def test_without_the_map_the_swap_is_every_row(tmp_path):
    """The swap unmapped is a difference in every row - never hidden."""
    pa, pb = _map_files(tmp_path)
    result = run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t")
    assert len(result.diffs) == 100 and not result.identical


@pytest.mark.parametrize("column_map", [{"a": "b"}, {"a": "b", "b": "b"}, {"a": "nope"}])
def test_a_map_that_cannot_be_honoured_is_refused(tmp_path, column_map):
    """Half a swap, two columns onto one, a column B lacks."""
    pa, pb = _map_files(tmp_path)
    with pytest.raises(ValueError, match="--map"):
        run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t", column_map=column_map)


# ========================================================= connection strings


@pytest.fixture
def two_files(tmp_path):
    """A base file and one with row 5 changed."""
    ddl = "create table t (id int, v varchar)"
    rows = "insert into t select i, 'v' || i from range(1, 101) r(i)"
    base = duck_file(tmp_path, "base", ddl, rows)
    chg = duck_file(tmp_path, "chg", ddl, rows, "update t set v = 'x' where id = 5")
    return base, chg


def _cli_diff(a_url: str, b_url: str) -> tuple[int, str, str]:
    """`parity diff --json` of main.t keyed on id."""
    return run_cli(
        "diff", "--a", a_url, "--a-table", "t", "--b", b_url, "--b-table", "t", "--key", "id", "--json"
    )


def _windows_spellings(path: str) -> list[str]:
    """Every way a user may write one Windows path into the URL."""
    fwd = path.replace("\\", "/")
    out = ["duckdb:///" + path, "duckdb:///" + fwd, "duckdb://" + fwd, "DUCKDB:///" + path]
    if os.name == "nt" and path[1:2] == ":":
        out.append("duckdb:///" + "\\\\localhost\\" + path[0] + "$" + path[2:])  # UNC, admin share
        out.append("duckdb:///" + "\\\\?\\" + path)  # extended-length prefix
    return out


def test_every_windows_spelling_of_the_path_finds_the_change(two_files):
    """Drive letters, backslashes, forward slashes, two or three slashes,
    UNC and \\\\?\\ prefixes: each opens the same file and finds row 5."""
    base, chg = two_files
    for url in _windows_spellings(chg):
        code, out, err = _cli_diff("duckdb:///" + base, url)
        if code == 2 and "\\\\localhost" in url and "not found" in err:
            continue  # no admin share on this machine
        assert code == 1, (url, err)
        diffs = json.loads(out)["differences"]
        assert [(d["key"], d["kind"]) for d in diffs] == [(5, "different")], url


def test_cli_exit_codes_and_json_follow_the_filter(two_files):
    """Row 5 differs. Filtered past it: exit 0, identical, the filter in the
    JSON. Filtered onto it: exit 1, exactly row 5. --quiet prints nothing on
    stdout but keeps the code."""
    base, chg = two_files
    common = ["diff", "--a", "duckdb:///" + base, "--a-table", "t", "--b", "duckdb:///" + chg,
              "--b-table", "t", "--key", "id"]
    code, out, err = run_cli(*common, "--where", "id > 5", "--json")
    j = json.loads(out)
    assert code == 0 and j["identical"] is True and j["where_a"] == "id > 5", err
    code, out, err = run_cli(*common, "--where", "id <= 5", "--json")
    j = json.loads(out)
    assert code == 1 and [(d["key"], d["kind"]) for d in j["differences"]] == [(5, "different")]
    code, out, _ = run_cli(*common, "--quiet")
    assert code == 1 and out == ""


def test_a_quoted_windows_path_is_refused_clearly(two_files):
    """Explorer's "Copy as path" adds quotes: a clear 'not found' naming the
    side and the path, exit 2."""
    base, chg = two_files
    code, _, err = _cli_diff("duckdb:///" + base, 'duckdb:///"' + chg + '"')
    assert code == 2 and "[side B: duckdb] database file not found" in err


def test_a_path_with_spaces_unicode_and_a_hash_opens(tmp_path, two_files):
    """`dir with space/ünïcødé 名前/my file#1.duckdb`."""
    base, chg = two_files
    d = tmp_path / "dir with space" / "ünïcødé 名前"
    d.mkdir(parents=True)
    fancy = d / "my file#1.duckdb"
    fancy.write_bytes(Path(chg).read_bytes())
    code, out, err = _cli_diff("duckdb:///" + base, "duckdb:///" + str(fancy))
    assert code == 1, err
    assert [d["key"] for d in json.loads(out)["differences"]] == [5]


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda p: p.write_bytes(b""), "not a valid"),
        (lambda p: p.write_text("hello\n" * 100), "not a valid"),
        (lambda p: p.mkdir(), "could not connect"),
    ],
    ids=["empty-file", "text-file", "directory"],
)
def test_a_path_that_is_not_a_database_is_refused_naming_the_side(tmp_path, two_files, make, message):
    """Exit 2, side named, and an empty file is left untouched (no write)."""
    base, _ = two_files
    target = tmp_path / "not_a_db.duckdb"
    make(target)
    code, out, err = _cli_diff("duckdb:///" + base, "duckdb:///" + str(target))
    assert code == 2
    assert "[side B: duckdb]" in err and message in err
    if target.is_file():
        assert not os.path.exists(str(target) + ".wal")


@pytest.mark.parametrize("url", ["duckdb:///:memory:", "duckdb://", "duckdb:///"])
def test_an_in_memory_side_is_an_empty_database(two_files, url):
    """`:memory:` and an empty path open a fresh empty database: table not found."""
    base, _ = two_files
    code, _, err = _cli_diff("duckdb:///" + base, url)
    assert code == 2 and "[side B: duckdb] table not found" in err


@pytest.mark.parametrize("prefix", ["duckdb:", "duckdb:/"])
def test_a_url_without_the_double_slash_gets_a_clear_error(two_files, prefix):
    """Refused, naming the expected grammar - not an IndexError's text."""
    base, chg = two_files
    code, _, err = _cli_diff("duckdb:///" + base, prefix + chg)
    assert code == 2
    assert "index out of range" not in err
    assert "duckdb:///" in err


SECRET = "rthd_SECRET_motherduck_token_0123456789"


@pytest.mark.parametrize(
    "url",
    [
        f"duckdb:///md:mydb?motherduck_token={SECRET}",
        f"duckdb://md:mydb?motherduck_token={SECRET}",
        f"duckdb:///md:?token={SECRET}",
    ],
)
def test_a_motherduck_url_is_refused_without_printing_its_token(two_files, url):
    """MotherDuck is not supported (it is a network service); the refusal
    must say so and must never echo the token."""
    base, _ = two_files
    code, out, err = _cli_diff("duckdb:///" + base, url)
    assert code == 2
    assert SECRET not in out + err


def test_a_motherduck_url_never_reaches_the_network(two_files, monkeypatch):
    """Whatever the message, `md:` must not be handed to duckdb.connect (which
    would load the motherduck extension and log in)."""
    calls: list = []
    real = duckdb.connect

    def spy(database=":memory:", *a, **kw):

        """Record what duckdb.connect is asked to open."""
        calls.append(database)
        return real(database, *a, **kw)

    monkeypatch.setattr(duckdb, "connect", spy)
    base, _ = two_files
    code, _, _ = _cli_diff("duckdb:///" + base, f"duckdb:///md:mydb?motherduck_token={SECRET}")
    assert code == 2
    assert not [c for c in calls if str(c).startswith("md:")]


def test_a_file_written_by_a_newer_duckdb_is_refused_naming_the_side(tmp_path):
    """A storage version this library cannot read: a clear refusal (the 2.0
    case, 'version number 999', verified with the 2.0 wheel); simulated here by
    a header from the future when no newer library is installed."""
    newer = os.environ.get("PARITY_RTHD_DUCKDB_NEWER_PYTHON")
    if not newer:
        pytest.skip("set PARITY_RTHD_DUCKDB_NEWER_PYTHON to a python with a newer duckdb")
    path = str(tmp_path / "newer.duckdb")
    subprocess.run(
        [newer, "-c", "import duckdb, sys; c = duckdb.connect(sys.argv[1]); c.execute('create table t (id int)'); c.close()", path],
        check=True,
    )
    with pytest.raises(ValueError, match=r"^\[side B: duckdb\] could not connect"):
        get_dialect(f"duckdb:///{path}", side="B")


# ============================================================ shared instances
#
# DuckDB's Python client shares one database instance per file within a
# process: a library caller who already holds a read-only connection hands
# the dialect its GLOBAL settings. None of these may change the verdict.

GLOBALS = [
    "SET GLOBAL Calendar = 'japanese'",
    "SET GLOBAL default_collation = 'nocase'",
    "SET GLOBAL TimeZone = 'Asia/Tokyo'",
    "SET GLOBAL default_null_order = 'nulls_first'",
    "SET GLOBAL default_order = 'desc'",
    "SET GLOBAL threads = 1",
    "SET GLOBAL enable_external_access = false",
    "SET GLOBAL ieee_floating_point_ops = false",
    "SET GLOBAL integer_division = true",
    "SET GLOBAL preserve_insertion_order = false",
    "SET GLOBAL errors_as_json = true",
]


@pytest.mark.parametrize("setting", GLOBALS)
def test_global_settings_of_a_shared_instance_do_not_change_the_verdict(tmp_path, setting):
    """Keys 'A' and 'a' stay two keys under a nocase default collation; the
    planted changes (row 5, key 'A') are found under every setting."""
    mk = (
        "create table t (id int, k varchar, tz timestamptz, s varchar)",
        "insert into t select i, 'k' || i, timestamptz '2020-06-15 12:00:00+00' + to_days(i), 's' || i from range(1, 101) r(i)",
    )
    pa = duck_file(tmp_path, "a", *mk, "insert into t values (1000, 'A', '2020-01-01 00:00:00+00', 'x'), (1001, 'a', '2020-01-01 00:00:00+00', 'x')")
    pb = duck_file(
        tmp_path, "b", *mk,
        "insert into t values (1000, 'A', '2020-01-01 00:00:00+00', 'X'), (1001, 'a', '2020-01-01 00:00:00+00', 'x')",
        "update t set s = 'S5' where id = 5",
    )
    held = [duckdb.connect(p, read_only=True) for p in (pa, pb)]
    try:
        for con in held:
            try:
                con.execute(setting)
            except duckdb.Error as exc:
                pytest.skip(f"not a global setting in DuckDB {duckdb.__version__}: {exc}")
        by_id = run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t")
        by_k = run_diff(open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t", key="k")
    finally:
        for con in held:
            con.close()
    assert found(by_id) == [(5, "different", ("s",)), (1000, "different", ("s",))]
    assert found(by_k) == [("A", "different", ("s",)), ("k5", "different", ("s",))]


def test_an_in_process_writer_is_refused_naming_the_side(tmp_path):
    """A library caller holding the file read-write in the same process: the
    read-only side cannot share that instance and says so - it never reads a
    table the writer is changing under it."""
    p = duck_file(tmp_path, "a", "create table t (id int)", "insert into t values (1)")
    rw = duckdb.connect(p)
    try:
        with pytest.raises(ValueError, match=r"^\[side A: duckdb\] could not connect"):
            open_duckdb(p, "A")
    finally:
        rw.close()


def test_a_locked_configuration_is_refused_naming_the_side(tmp_path):
    """`lock_configuration` on a shared instance stops the extension pins:
    the dialect refuses rather than run unpinned."""
    p = duck_file(tmp_path, "a", "create table t (id int)", "insert into t values (1)")
    held = duckdb.connect(p, read_only=True)
    try:
        held.execute("SET GLOBAL lock_configuration = true")
        with pytest.raises(ValueError, match=r"^\[side A: duckdb\] could not connect.*locked"):
            open_duckdb(p, "A")
    finally:
        held.close()


# ================================================================== versions
#
# `rthd_duck_probe.py` under every interpreter of PARITY_RTG_DUCKDB_PYTHONS
# (one virtualenv per DuckDB release; round H ran 0.10.3, 1.0.0, 1.1.0, 1.1.3,
# 1.2.0, 1.2.2, 1.3.0-1.3.2, 1.4.0-1.4.5, 1.5.0-1.5.6 and 2.0.0).

from .rtg_servers import DUCKDB_PYTHONS  # noqa: E402


def _version_of(python: str) -> tuple[int, ...]:
    """The duckdb version of `python`, (0,) if unusable."""
    try:
        out = subprocess.run(  # noqa: S603 - a configured interpreter
            [python, "-c", "import duckdb; print(duckdb.__version__)"],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout.strip()
    except Exception:
        return (0,)
    return tuple(int(p) for p in out.split(".")[:3] if p.isdigit())


_PYTHONS = [(p, _version_of(p)) for p in DUCKDB_PYTHONS]

def _version_params():
    """One param per interpreter (RT-HD11 was a false match on 1.3-1.4)."""
    out = []
    for python, v in _PYTHONS:
        marks = []
        if v < (1, 3):
            marks.append(pytest.mark.skip(reason="refused at connect (DuckDB < 1.3)"))
        out.append(pytest.param(python, v, marks=marks, id="duckdb-" + ".".join(map(str, v))))
    return out or [pytest.param(None, (0,), id="no-duckdb-venvs")]


def _run_probe(python: str, tmp_path: Path) -> dict:
    """`rthd_duck_probe.py` under `python`; its JSON."""
    here = Path(__file__).resolve().parent
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(here.parent.parent / "src"), str(here.parent)]))
    proc = subprocess.run(  # noqa: S603 - a configured interpreter
        [python, str(here / "rthd_duck_probe.py"), str(tmp_path)],
        capture_output=True, text=True, env=env, timeout=600, encoding="utf-8",
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RTHD-JSON ")]
    assert lines, f"probe failed ({proc.returncode}): {proc.stderr[-2000:]}"
    return json.loads(lines[-1][len("RTHD-JSON "):])


@pytest.mark.parametrize(("python", "version"), _version_params())
def test_every_supported_duckdb_release_finds_each_planted_value(python, version, tmp_path):
    """Empty list against NULL, list quoting, TIMETZ offset seconds, UNION
    tags, nanoseconds, interval months, decimal rounding: row 1 differs in
    each case, and every supported release must find it."""
    if python is None:
        pytest.skip("PARITY_RTG_DUCKDB_PYTHONS not set")
    got = _run_probe(python, tmp_path)["cases"]
    wrong = {k: v for k, v in got.items() if v.get("found") != [[1, "different"]]}
    assert not wrong, wrong


@pytest.mark.parametrize(
    ("python", "version"),
    [
        pytest.param(p, v, id="duckdb-" + ".".join(map(str, v)))
        for p, v in _PYTHONS
        if (0,) < v < (1, 3)
    ]
    or [pytest.param(None, (0,), id="none-old")],
)
def test_every_release_below_the_minimum_is_refused(python, version, tmp_path):
    """0.10, 1.0, 1.1, 1.2: refused at connect, naming the version, in every case."""
    if python is None:
        pytest.skip("no DuckDB < 1.3 interpreter configured")
    got = _run_probe(python, tmp_path)["cases"]
    assert all("is not supported" in v.get("refused", "") for v in got.values()), got
