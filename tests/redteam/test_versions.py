"""Red team: engine versions - PostgreSQL 9.5-18, MySQL 8.0/8.4/9, SQL Server
2019/2022 and its compatibility levels, DuckDB 1.0-2.0.

Every version must render every value of the conformance harness
(`tests/conformance`) byte for byte as the reference version does, agree with
the FLOAT spec on the adversarial sample, and find exactly the planted
differences of the CLAUDE.md battery (a changed decimal, a deleted row, an
inserted row at key 999,999,999, NULL against '', FALSE against NULL).
Versions the dialects refuse must refuse loudly and say why.

Servers are the Docker containers of `rtg_servers.py`; old DuckDB libraries
run in their own virtualenvs (`PARITY_RTG_DUCKDB_PYTHONS`), through
`rtg_duck_probe.py`. Everything skips cleanly when absent. Confirmed bugs are
`xfail(strict=True)` with an `RT-G<n>` id.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from conftest import open_duckdb
from float_sample import adversarial_doubles, render

from parity.dialects.base import canonical_float_text, get_dialect

from .rtg_servers import (
    DUCKDB_PYTHONS,
    MSSQL_ADMIN_URL,
    MSSQL_VERSION_URLS,
    MYSQL97_ROOT_URL,
    MYSQL_VERSION_URLS,
    PG_REFERENCE_URL,
    PG_VERSION_URLS,
    conformance_path,
    duck_file,
    found,
    mssql_drop_database,
    mssql_exec,
    mysql_connect,
    mysql_run,
    pg_connect,
    run_diff,
    short,
)

SAMPLE = adversarial_doubles()

#: What the planted battery must report, engine X (side A) against DuckDB.
BATTERY = [
    (7, "different", ("amount",)),
    (50, "only_in_a", ("amount", "flag", "name")),
    (100, "different", ("name",)),
    (200, "different", ("flag",)),
    (999999999, "only_in_b", ("amount", "flag", "name")),
]


def _battery_duck(tmp_path: Path, flag: str = "boolean") -> str:
    """The DuckDB side of the battery: 300 rows with the five planted changes."""
    rows = "select i, i * 1.25, 'n' || i, i % 2 = 0 from range(1, 301) r(i)"
    return duck_file(
        tmp_path,
        "battery",
        f"create table t (id integer, amount decimal(12,2), name varchar, flag {flag})",
        f"insert into t {rows}",
        "update t set amount = 9.99 where id = 7",
        "delete from t where id = 50",
        "insert into t values (999999999, 1, 'new', true)",
        "update t set name = '' where id = 100",
        "update t set flag = null where id = 200",
    )


# --------------------------------------------------------- conformance runs

_COV_CACHE: dict[str, dict] = {}


def _cov_cells(engine_cls, url: str) -> dict:
    """{(native, key): (status, text)} of one conformance run at scale 6, cached."""
    if url not in _COV_CACHE:
        conformance_path()
        from cov_harness import run

        eng = engine_cls(url)
        try:
            runs = run(eng, (6,))
        finally:
            eng.drop_all()
        _COV_CACHE[url] = {(c.native, c.key): (c.status, c.text) for c in runs[6].cells}
    return _COV_CACHE[url]


def _cov_mismatch(got: dict, ref: dict) -> list[str]:
    """Cells whose status or text differs from the reference, or that failed."""
    bad = [
        f"{k}: {got.get(k)!r} vs reference {ref.get(k)!r}"
        for k in ref
        if got.get(k) != ref[k]
    ]
    bad += [f"{k}: {v!r}" for k, v in got.items() if v[0] in ("disagree", "error")]
    bad += [f"{k}: only here {got[k]!r}" for k in got if k not in ref]
    return bad


# =============================================================== PostgreSQL


def _pg_version(url: str) -> int:
    """`server_version_num` of the server at `url`, or skip if none answers."""
    conn = pg_connect(url)
    try:
        return int(conn.execute("show server_version_num").fetchone()[0])
    finally:
        conn.close()


def _pg_battery_table(url: str) -> str:
    """The battery's side A on PostgreSQL: `rtg.battery`, 300 rows."""
    conn = pg_connect(url)
    try:
        conn.execute("create schema if not exists rtg")
        conn.execute("drop table if exists rtg.battery")
        conn.execute(
            "create table rtg.battery (id int primary key, amount numeric(12,2), "
            "name text, flag bool)"
        )
        conn.execute(
            "insert into rtg.battery select i, i * 1.25, 'n' || i, i % 2 = 0 "
            "from generate_series(1, 300) i"
        )
        conn.execute("update rtg.battery set name = null where id = 100")
        conn.execute("update rtg.battery set flag = false where id = 200")
        return "rtg.battery"
    finally:
        conn.close()


@pytest.mark.parametrize("url", [*PG_VERSION_URLS, PG_REFERENCE_URL], ids=short)
def test_pg_version_finds_the_planted_battery(url, tmp_path):
    """Every PostgreSQL from 9.6 on finds exactly the five planted differences
    against DuckDB, on a table without FLOAT."""
    if _pg_version(url) < 90600:
        pytest.skip("below 9.6: see RT-G9")
    table = _pg_battery_table(url)
    result = run_diff(
        get_dialect(url, side="A"),
        open_duckdb(_battery_duck(tmp_path), "B"),
        table,
        "main.t",
    )
    assert found(result) == BATTERY


@pytest.mark.parametrize("url", PG_VERSION_URLS, ids=short)
def test_pg_version_renders_like_the_reference(url):
    """PostgreSQL 12-17 render every conformance value byte for byte as the
    reference (18) does, with no disagreement against the rules."""
    if _pg_version(url) < 120000:
        pytest.skip("FLOAT needs 12+; see test_pg_before_12_refuses_float")
    conformance_path()
    from cov_engines import PostgresCov

    ref = _cov_cells(PostgresCov, PG_REFERENCE_URL)
    got = _cov_cells(PostgresCov, url)
    bad = _cov_mismatch(got, ref)
    assert not bad, f"{len(bad)} cells:\n" + "\n".join(bad[:20])


@pytest.mark.parametrize("url", [*PG_VERSION_URLS, PG_REFERENCE_URL], ids=short)
def test_pg_version_renders_the_adversarial_floats_as_the_spec(url):
    """1,565 hard doubles on each PostgreSQL 12+, at scale 6."""
    if _pg_version(url) < 120000:
        pytest.skip("FLOAT needs 12+")
    d = get_dialect(url, side="A")
    try:
        got = render(d, "postgres", SAMPLE)
    finally:
        d.close()
    bad = [
        (x, g)
        for x, g in zip(SAMPLE, got, strict=True)
        if g != canonical_float_text(x, 6)
    ]
    assert not bad, bad[:5]


@pytest.mark.parametrize("url", PG_VERSION_URLS, ids=short)
def test_pg_before_12_refuses_float_naming_the_version(url):
    """9.6, 10 and 11 print 17 significant digits, not the shortest repr: a
    FLOAT column is refused before any query, naming the server version."""
    version = _pg_version(url)
    if not 90600 <= version < 120000:
        pytest.skip("only 9.6-11")
    conn = pg_connect(url)
    conn.execute("create schema if not exists rtg")
    conn.execute("drop table if exists rtg.f")
    conn.execute("create table rtg.f (id int primary key, x float8)")
    conn.execute("insert into rtg.f values (1, 0.1)")
    conn.close()
    a = get_dialect(url, side="A")
    with pytest.raises(ValueError, match=rf"side A.*PostgreSQL 12 or newer.*{version}"):
        run_diff(a, get_dialect(url, side="B"), "rtg.f", "rtg.f")


@pytest.mark.xfail(
    strict=True,
    reason="RT-G9: PostgreSQL 9.5 and older cannot be connected at all - the "
    "session pins include idle_in_transaction_session_timeout (9.6+) - and the "
    "error is 'unrecognized configuration parameter', naming no version; the "
    "minimum server version is not documented",
)
@pytest.mark.parametrize("url", PG_VERSION_URLS, ids=short)
def test_rt_g9_pg_95_is_refused_naming_the_version(url):
    """A 9.5 legacy server (end of life 2021, still common behind a
    migration): parity must say it needs a newer server, not trip over a
    setting name."""
    if _pg_version(url) >= 90600:
        pytest.skip("only below 9.6")
    with pytest.raises(ValueError, match=r"side A.*(version|9\.6)"):
        get_dialect(url, side="A").close()


# ==================================================================== MySQL


def _mysql_version(url: str) -> str:
    """`version()` of the MySQL at `url`, or skip."""
    conn = mysql_connect(url)
    try:
        return str(mysql_run(conn, "select version()")[0][0][0])
    finally:
        conn.close()


def _mysql_has_md5(url: str) -> bool:
    """Does this server have MD5()? MySQL 9.7 moved it (and SHA1) into the
    optional component_classic_hashing, which a default install lacks."""
    conn = mysql_connect(url)
    try:
        mysql_run(conn, "select md5('')")
        return True
    except Exception:  # error 1305: FUNCTION <db>.md5 does not exist
        return False
    finally:
        conn.close()


@pytest.mark.parametrize("url", MYSQL_VERSION_URLS, ids=short)
def test_mysql_version_finds_the_planted_battery(url, tmp_path):
    """MySQL 8.0, 8.4 LTS and 9.x: the battery against DuckDB (flag as
    TINYINT on both sides - MySQL's BOOLEAN is TINYINT(1))."""
    _mysql_version(url)
    if not _mysql_has_md5(url):
        pytest.skip("no MD5() on this server: RT-G13")
    assert found(_mysql_battery(url, tmp_path)) == BATTERY


@pytest.mark.xfail(
    strict=True,
    reason="RT-G13: MySQL 9.7 removed MD5() from the server into the optional "
    "component_classic_hashing; without it every diff dies in the first checksum "
    "with 'FUNCTION <db>.md5 does not exist', which names neither the cause nor "
    "the remedy, and nothing says so before the walk starts",
)
@pytest.mark.parametrize("url", MYSQL_VERSION_URLS, ids=short)
def test_rt_g13_mysql_without_md5_is_refused_naming_the_component(url, tmp_path):
    """A default MySQL 9.7 install: parity cannot hash there, and must say
    why (the missing component) and what to do (INSTALL COMPONENT, or
    another engine path), naming the side."""
    _mysql_version(url)
    if _mysql_has_md5(url):
        pytest.skip("this server has MD5()")
    with pytest.raises(ValueError, match=r"side A.*classic_hashing"):
        _mysql_battery(url, tmp_path)


def test_mysql_97_with_classic_hashing_finds_the_planted_battery(tmp_path):
    """With component_classic_hashing installed (by root, then uninstalled),
    MySQL 9.7 hashes exactly as 8.0 does: the battery is found."""
    root = MYSQL97_ROOT_URL
    conn = mysql_connect(root)
    try:
        version = str(mysql_run(conn, "select version()")[0][0][0])
        if not version.startswith("9."):
            pytest.skip(f"MySQL {version} is not 9.x")
        native = _mysql_has_md5(root)
        if not native:
            mysql_run(conn, "install component 'file://component_classic_hashing'")
        try:
            result = _mysql_battery(root.replace("root:rtg@", "rtg:rtg@"), tmp_path)
        finally:
            if not native:
                mysql_run(conn, "uninstall component 'file://component_classic_hashing'")
    finally:
        conn.close()
    assert found(result) == BATTERY


def _mysql_battery(url: str, tmp_path: Path):
    """The battery, MySQL table `rtg_battery` (side A) against DuckDB."""
    conn = mysql_connect(url)
    try:
        mysql_run(
            conn,
            "drop table if exists rtg_battery",
            "create table rtg_battery (id int primary key, amount decimal(12,2), "
            "name varchar(20), flag tinyint) charset utf8mb4",
        )
        cur = conn.cursor()
        cur.executemany(
            "insert into rtg_battery values (%s, %s, %s, %s)",
            [(i, f"{i * 1.25:.2f}", f"n{i}", int(i % 2 == 0)) for i in range(1, 301)],
        )
        cur.close()
        mysql_run(
            conn,
            "update rtg_battery set name = null where id = 100",
            "update rtg_battery set flag = 0 where id = 200",
        )
        duck = _battery_duck(tmp_path, flag="tinyint")
        return run_diff(
            get_dialect(url, side="A"), open_duckdb(duck, "B"), "rtg_battery", "main.t"
        )
    finally:
        mysql_run(conn, "drop table if exists rtg_battery")
        conn.close()


@pytest.mark.parametrize("url", MYSQL_VERSION_URLS[:-1], ids=short)
def test_mysql_version_renders_like_the_reference(url):
    """MySQL 8.4 and 9.x render every conformance value as 8.0 does."""
    _mysql_version(url)
    _mysql_version(MYSQL_VERSION_URLS[-1])
    conformance_path()
    from cov_engines import MySQLCov

    ref = _cov_cells(MySQLCov, MYSQL_VERSION_URLS[-1])
    got = _cov_cells(MySQLCov, url)
    bad = _cov_mismatch(got, ref)
    assert not bad, f"{len(bad)} cells:\n" + "\n".join(bad[:20])


@pytest.mark.parametrize("url", MYSQL_VERSION_URLS, ids=short)
def test_mysql_version_renders_the_adversarial_floats_as_the_spec(url):
    """1,565 hard doubles on each MySQL version, at scale 6."""
    _mysql_version(url)
    d = get_dialect(url, side="A")
    try:
        got = render(d, "mysql", SAMPLE)
    finally:
        d.close()
    bad = [
        (x, g)
        for x, g in zip(SAMPLE, got, strict=True)
        if g != canonical_float_text(x, 6)
    ]
    assert not bad, bad[:5]


# =============================================================== SQL Server


class _Ms:
    """Conformance runs in a database of our own name at a chosen level."""

    @staticmethod
    def cells(url: str, compat: int | None, monkeypatch) -> dict:
        """The harness on `url` in database `rtg_cov_<compat>`, pre-created at
        that compatibility level (None: the server's default)."""
        conformance_path()
        import cov_engines

        key = f"{url}#{compat}"
        if key in _COV_CACHE:
            return _COV_CACHE[key]
        name = f"rtg_cov_{compat or 'default'}"
        monkeypatch.setattr(cov_engines, "MSSQL_DB", name)
        mssql_drop_database(url, name)
        create = [f"create database [{name}] collate Latin1_General_100_CI_AS_SC_UTF8"]
        if compat:
            create.append(f"alter database [{name}] set compatibility_level = {compat}")
        mssql_exec(url, "master", *create)
        try:
            return _ms_run(key, url)
        finally:
            mssql_drop_database(url, name)


def _ms_run(key: str, url: str) -> dict:
    """One SQL Server conformance run, cached under `key`."""
    if key not in _COV_CACHE:
        import cov_engines
        from cov_harness import run

        eng = cov_engines.MSSQLCov(url)
        try:
            runs = run(eng, (6,))
        finally:
            eng.drop_all()
        _COV_CACHE[key] = {(c.native, c.key): (c.status, c.text) for c in runs[6].cells}
    return _COV_CACHE[key]


def test_mssql_2019_renders_like_2022(monkeypatch):
    """SQL Server 2019 (its default level 150) renders every conformance value
    as 2022 (level 160) does."""
    old, new = MSSQL_VERSION_URLS[0], MSSQL_VERSION_URLS[-1]
    ref = _Ms.cells(new, None, monkeypatch)
    got = _Ms.cells(old, None, monkeypatch)
    bad = _cov_mismatch(got, ref)
    assert not bad, f"{len(bad)} cells:\n" + "\n".join(bad[:20])


@pytest.mark.parametrize(
    "compat",
    [
        pytest.param(
            c,
            marks=pytest.mark.xfail(
                strict=True,
                reason="RT-G5: below compatibility level 130 float -> decimal is not "
                "exact, so FLOAT renders off the canonical form",
            ),
        )
        if c < 130
        else c
        for c in (100, 110, 120, 130, 140, 150, 160)
    ],
)
def test_mssql_compatibility_level_renders_like_the_default(compat, monkeypatch):
    """Every compatibility level SQL Server 2022 accepts must render every
    conformance value as its default level (160) does. 130+ do; 100-120
    disagree on 31 FLOAT/REAL cells (RT-G5) and on nothing else."""
    ref = _Ms.cells(MSSQL_ADMIN_URL, None, monkeypatch)
    got = _Ms.cells(MSSQL_ADMIN_URL, compat, monkeypatch)
    bad = _cov_mismatch(got, ref)
    assert not bad, f"{len(bad)} cells:\n" + "\n".join(bad[:20])


@pytest.mark.parametrize("compat", [100, 110, 120])
def test_mssql_low_compatibility_level_differs_only_in_float(compat, monkeypatch):
    """Scope of RT-G5, pinned: at 100-120 the only cells that differ from the
    default level are FLOAT and REAL ones - DECIMAL, datetime and the rest are
    unaffected (datetime renders its display, independent of the level)."""
    ref = _Ms.cells(MSSQL_ADMIN_URL, None, monkeypatch)
    got = _Ms.cells(MSSQL_ADMIN_URL, compat, monkeypatch)
    natives = {k[0] for k in ref if got.get(k) != ref[k]}
    assert natives <= {"float", "real"}, natives


# =================================================================== DuckDB


def _duck_version(python: str) -> tuple[int, ...]:
    """The duckdb version installed for `python`, as a tuple (0,) if none."""
    try:
        out = subprocess.run(  # noqa: S603 - a configured interpreter
            [python, "-c", "import duckdb; print(duckdb.__version__)"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout.strip()
    except Exception:  # an unusable interpreter is skipped, not failed
        return (0,)
    return tuple(int(p) for p in out.split(".")[:3] if p.isdigit())


_DUCKS = [(p, _duck_version(p)) for p in DUCKDB_PYTHONS]


def _probe(python: str, mode: str, tmp_path: Path) -> dict:
    """Run `rtg_duck_probe.py <mode>` under `python`; its JSON result."""
    if not os.path.exists(python):
        pytest.skip(f"no interpreter at {python}")
    here = Path(__file__).resolve().parent
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(here.parent.parent / "src"), str(here.parent)]
    )
    proc = subprocess.run(  # noqa: S603 - a configured interpreter
        [python, str(here / "rtg_duck_probe.py"), mode, str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
        encoding="utf-8",
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RTG-JSON ")]
    assert lines, f"probe failed ({proc.returncode}): {proc.stderr[-2000:]}"
    return json.loads(lines[-1][len("RTG-JSON ") :])


def _ids(v: tuple[int, ...]) -> str:
    """A test id for a DuckDB version tuple."""
    return "duckdb-" + ".".join(map(str, v))


def _duck_params(
    affected: tuple[tuple[int, ...], tuple[int, ...]] | None = None, reason: str = ""
):
    """One param per configured interpreter; xfail(strict) for versions in
    the half-open range `affected` = (first bad, first fixed)."""
    out = []
    for python, version in _DUCKS:
        marks = []
        if affected and affected[0] <= version < affected[1]:
            marks.append(pytest.mark.xfail(strict=True, reason=reason))
        out.append(pytest.param(python, version, marks=marks, id=_ids(version)))
    return out or [pytest.param(None, (0,), id="no-duckdb-venvs")]


def _need(python):
    """Skip when no other-version interpreter is configured."""
    if python is None:
        pytest.skip("PARITY_RTG_DUCKDB_PYTHONS not set")


@pytest.mark.parametrize(("python", "version"), _duck_params())
def test_duckdb_version_finds_the_planted_battery(python, version, tmp_path):
    """The battery, DuckDB against DuckDB, under each library version."""
    _need(python)
    got = _probe(python, "scenarios", tmp_path)["battery"]
    assert got["found"] == [
        ["100", "different", ["name"]],
        ["200", "different", ["flag"]],
        ["50", "only_in_a", ["amount", "flag", "name"]],
        ["7", "different", ["amount"]],
        ["999999999", "only_in_b", ["amount", "flag", "name"]],
    ], got


#: Natives the comparison leaves out, by version, and why. Before 1.5 the
#: Python client binds an int beyond int64 through DOUBLE, so the harness's
#: HUGEINT values are already rounded when stored - a fixture artifact, not a
#: rendering. TIMESTAMP_NS on 1.1 is RT-G11, pinned on its own.
def _excluded(native: str, version: tuple[int, ...]) -> bool:
    """Is this native left out of the cross-version comparison at `version`?"""
    if native in ("HUGEINT", "UHUGEINT") and version < (1, 5):
        return True
    return native == "TIMESTAMP_NS" and (1, 1) <= version < (1, 2)


#: RT-G14's reason, shared by the two tests it fails.
_G14 = (
    "RT-G14: DuckDB 1.0 casts DECIMAL to a smaller scale by truncating, not "
    "rounding, so 2.0000005 renders 2.000000 (the spec and every other engine: "
    "2.000001) - false alarms across engines and false matches like "
    "2.0000005 = 2.0000000; pyproject accepts duckdb>=1.0"
)


@pytest.mark.parametrize(("python", "version"), _duck_params(((1, 0), (1, 1)), _G14))
def test_duckdb_version_renders_like_this_one(python, version, tmp_path):
    """Every ruled conformance value renders under each DuckDB version exactly
    as under the library this suite runs with. Unruled (UNKNOWN) types are
    compared as raw text and their text may change between releases: lists
    print `['a']` from 1.3 and `[a]` before - see RT-G10."""
    _need(python)
    conformance_path()
    import duckdb
    from cov_engines import DuckDBCov

    key = f"duckdb-{duckdb.__version__}"
    if key not in _COV_CACHE:
        from cov_harness import run

        (tmp_path / "here").mkdir()
        eng = DuckDBCov(str(tmp_path / "here"))
        try:
            runs = run(eng, (6,))
        finally:
            eng.drop_all()
        _COV_CACHE[key] = {
            f"{c.native}|{c.key}": [c.status, c.text] for c in runs[6].cells
        }
    ref = _COV_CACHE[key]
    other = tmp_path / "other"
    other.mkdir()
    got = _probe(python, "cov", other)["cells"]
    bad = []
    for k, (status, text) in ref.items():
        native = k.split("|", 1)[0]
        if status == "unruled" or _excluded(native, version) or k not in got:
            continue
        if got[k][1] != text:
            bad.append(f"{k}: {got[k]!r} vs {[status, text]!r}")
    assert not bad, f"{len(bad)} cells:\n" + "\n".join(bad[:20])


@pytest.mark.parametrize(
    ("python", "version"),
    _duck_params(
        ((0,), (1, 3)),
        "RT-G10: before DuckDB 1.3 a list's text does not quote its strings, so "
        "['NULL'] equals [NULL] and ['a, b'] equals ['a', 'b'] - false matches "
        "DuckDB to DuckDB; pyproject accepts duckdb>=1.0",
    ),
)
def test_rt_g10_duckdb_list_text_is_unambiguous(python, version, tmp_path):
    """A list column (unmapped, compared as raw text): a list holding the
    string 'NULL' against one holding NULL, and ['a, b'] against ['a', 'b'],
    must both differ."""
    _need(python)
    got = _probe(python, "scenarios", tmp_path)["lists"]
    assert got["found"] == [["1", "different", ["l"]], ["2", "different", ["l"]]], got


@pytest.mark.parametrize(
    ("python", "version"),
    _duck_params(
        ((1, 1), (1, 2)),
        "RT-G11: DuckDB 1.1 renders a TIMESTAMP_NS before the epoch rounded toward "
        "zero (1969-12-31 23:59:59.9999999 as 1970-01-01 00:00:00.000000, not the "
        "truncated .999999), and its sample select raises an internal error",
    ),
)
def test_rt_g11_duckdb_nanosecond_timestamp_before_the_epoch(python, version, tmp_path):
    """1969-12-31 23:59:59.9999999 truncates to .999999 at microseconds, so
    against 1970-01-01 00:00:00 it must differ, and the sample check must run."""
    _need(python)
    got = _probe(python, "scenarios", tmp_path)["ns_before_epoch"]
    assert got["found"] == [["1", "different", ["ts"]]], got
    assert got["sample_error"] is None, got["sample_error"]


@pytest.mark.parametrize(("python", "version"), _duck_params(((1, 0), (1, 1)), _G14))
def test_rt_g14_duckdb_decimal_rounds_half_away_from_zero(python, version, tmp_path):
    """DECIMAL(12,7) 2.0000005 against 2.0000000, and -1.2345665 against
    -1.2345660, DuckDB to DuckDB: at six places the first of each pair rounds
    away (2.000001, -1.234567), so both rows must differ."""
    _need(python)
    got = _probe(python, "scenarios", tmp_path)["decimal_half"]
    assert got["found"] == [["1", "different", ["x"]], ["2", "different", ["x"]]], got
