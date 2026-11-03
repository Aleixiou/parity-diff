"""Red team: concurrency, sessions and environment, against live engines.

Every test here plants a difference (or a mid-walk write) and asserts what a
trustworthy parity tool must report. Confirmed bugs are `xfail(strict=True)`
with an `RT-D<n>` id; the passing tests pin behaviour that was attacked and
held.

Session and database defaults are planted the way a real server would carry
them, *before* the dialect's own session statements run:

- PostgreSQL: libpq's `options` URL parameter (`-c name=value`), which is what
  `PGOPTIONS`, `ALTER ROLE ... SET` and `ALTER DATABASE ... SET` also feed - a
  backend starts with these values and the dialect's `connect` runs after.
- MySQL: a hook that runs one statement right after the handshake and before
  mysql-connector's own post-connection statements (`SET NAMES`, autocommit),
  which is exactly where a server's `init_connect` runs.
- SQL Server: a login and a database created for the test (`rt_live_*`), with
  their own DEFAULT_LANGUAGE and database-level ANSI options.

Mid-walk writes are deterministic: the dialect's `segment_checksums` is wrapped
so that the write runs, from another connection, right after the first
checksum query returns - between bisection levels, never racing a sleep.

Everything this module creates is prefixed `rt_live_` and dropped afterwards.
No server-wide setting is touched.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import textwrap
import uuid
from collections.abc import Callable
from urllib.parse import quote, unquote, urlparse, urlunparse

import pytest
from conftest import duckdb_write, open_duckdb, open_mssql, open_mysql, open_pg

from parity.dialects.base import get_dialect
from parity.engine import diff

RUN = uuid.uuid4().hex[:8]
#: Per-run names, so concurrent red-team runs on the shared servers never
#: touch each other's objects.
SCHEMA = f"rt_live_rt_{RUN}"
SHADOW = f"rt_live_shadow_{RUN}"


# ------------------------------------------------------------------ helpers


def after_first_checksum(dialect, action: Callable[[], None]) -> list[bool]:
    """Run `action` once, right after `dialect`'s first checksum query returns.

    That is the gap between bisection level 1 and everything after it - the
    window in which a live table can change underneath the walk. The engine
    calls `segment_checksums` through the instance, so an instance attribute
    is enough; no sleeps, no races.
    """
    original = dialect.segment_checksums
    fired: list[bool] = []

    def wrapped(*args, **kwargs):
        out = original(*args, **kwargs)
        if not fired:
            fired.append(True)
            action()
        return out

    dialect.segment_checksums = wrapped
    return fired


def pg_options(url: str, **gucs: str) -> str:
    """`url` with libpq `options=-c k=v ...` - a backend-start session default."""
    opts = " ".join(f"-c {k}={v}" for k, v in gucs.items())
    sep = "&" if "?" in url else "?"
    return url + sep + "options=" + quote(opts, safe="")


def found(result) -> list[tuple]:
    """(key, kind, columns) of every difference, sorted by key."""
    return sorted(
        ((d.key, d.kind, tuple(d.columns)) for d in result.diffs), key=lambda t: t[0]
    )


def duck_file(tmp_path, *statements: str) -> str:
    """Build a DuckDB file with `statements` and close it."""
    path = str(tmp_path / f"rt_live_{uuid.uuid4().hex[:6]}.duckdb")
    con = duckdb_write(path)
    try:
        for sql in statements:
            con.execute(sql)
    finally:
        con.close()
    return path


def run_diff(a, b, a_table: str, b_table: str, **kw):
    """Diff and always close both sides."""
    kw.setdefault("key", "id")
    try:
        return diff(a, b, a_table, b_table, **kw)
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------- PostgreSQL


@pytest.fixture(scope="module")
def pgw(pg_url):
    """An autocommit writer on the shared `parity` database, and cleanup."""
    import psycopg

    conn = psycopg.connect(pg_url, autocommit=True)
    for schema in (SCHEMA, SHADOW):
        conn.execute(f"create schema if not exists {schema}")
    yield conn
    for schema in (SCHEMA, SHADOW):
        conn.execute(f"drop schema if exists {schema} cascade")
    conn.execute(f"drop table if exists public.rt_live_sp_{RUN}")
    conn.close()


@pytest.mark.xfail(
    strict=True,
    reason="RT-D1: standard_conforming_strings=off turns the '\\N' sentinel into "
    "'N', so a PostgreSQL NULL matches the string 'N' (false match)",
)
def test_pg_standard_conforming_strings_off_makes_null_equal_the_letter_n(
    pg_url, pgw, tmp_path
):
    """The NULL sentinel is the literal `'\\N'`. With standard_conforming_strings
    off - a role, database or PGOPTIONS default, and the norm on servers that
    predate 9.1 - PostgreSQL processes backslash escapes in plain literals and
    `'\\N'` becomes `'N'`. A NULL on side A then renders exactly like the
    one-letter string `N` on side B. Planted: A holds NULL, B holds 'N'."""
    t = f"{SCHEMA}.scs_{RUN}"
    pgw.execute(f"create table {t} (id int primary key, v text)")
    pgw.execute(f"insert into {t} values (1, null), (2, 'x')")
    duck = duck_file(
        tmp_path,
        "create table t (id int, v varchar)",
        "insert into t values (1, 'N'), (2, 'x')",
    )
    a = open_pg(pg_options(pg_url, standard_conforming_strings="off"))
    result = run_diff(a, open_duckdb(duck), t, "main.t")
    assert found(result) == [(1, "different", ("v",))]


@pytest.mark.xfail(
    strict=True,
    reason="RT-D2: an unqualified table is introspected in `public` but read "
    "through search_path, so a same-named table in another schema is compared "
    "(false match)",
)
def test_pg_search_path_reads_a_different_table_than_it_introspects(
    pg_url, pgw, tmp_path
):
    """`--a-table orders` is documented to mean `public.orders`, and `columns()`
    does look there - but every checksum and fetch says `from "orders"`, which
    the server resolves through `search_path`. The default search_path is
    `"$user", public`, so a login with a scratch schema named after itself (a
    common sandbox pattern) compares its scratch copy; here a role default
    puts the shadow schema first. public holds the REAL value; the shadow copy
    matches side B; the tool must still report the public table's difference."""
    name = f"rt_live_sp_{RUN}"
    pgw.execute(f"create table public.{name} (id int primary key, v text)")
    pgw.execute(f"insert into public.{name} values (1, 'REAL'), (2, 'x')")
    pgw.execute(f"create table {SHADOW}.{name} (id int primary key, v text)")
    pgw.execute(f"insert into {SHADOW}.{name} values (1, 'copy'), (2, 'x')")
    duck = duck_file(
        tmp_path,
        f"create table {name} (id int, v varchar)",
        f"insert into {name} values (1, 'copy'), (2, 'x')",
    )
    a = open_pg(pg_options(pg_url, search_path=f"{SHADOW},public"))
    result = run_diff(a, open_duckdb(duck), name, name)
    assert found(result) == [(1, "different", ("v",))]


@pytest.mark.xfail(
    strict=True,
    reason="RT-D2: qualify() leaves an unqualified table unqualified, so the "
    "schema read is the session's, not the one columns() introspected",
)
@pytest.mark.parametrize(
    "dialect_path, schema",
    [
        ("parity.dialects.postgres_dialect:PostgresDialect", "public"),
        ("parity.dialects.snowflake_dialect:SnowflakeDialect", "PUBLIC"),
    ],
)
def test_unqualified_tables_are_read_from_the_schema_they_were_introspected_in(
    dialect_path, schema
):
    """Offline form of RT-D2. On Snowflake a URL without a schema introspects
    `PUBLIC` while the session reads the user's DEFAULT_NAMESPACE."""
    import importlib

    module, cls = dialect_path.split(":")
    d = getattr(importlib.import_module(module), cls)(side="A")
    assert d.split_table("orders", d.default_schema)[0] == schema
    assert d.qualify("orders") == f"{d.quote(schema)}.{d.quote('orders')}"


#: Rows at the edges of both DST transitions in Europe/Zurich (2024-03-31 01:00
#: UTC, 2024-10-27 01:00 UTC) and America/New_York (2024-03-10, 2024-11-03).
DST_ROWS = [
    (
        1,
        "2024-03-31 00:59:59.999999+00",
        "2024-03-31 02:30:00",
        0.1,
        "1234.5678",
        "café 😀",
    ),
    (
        2,
        "2024-03-31 01:00:00+00",
        "2024-03-31 01:59:59",
        1e15 + 0.5,
        "-0.00005",
        "Zürich",
    ),
    (3, "2024-10-27 00:30:00+00", "2024-10-27 02:30:00", -2.5, "2.5", "a"),
    (4, "2024-10-27 01:30:00+00", "2024-10-27 02:30:00", 1 / 3, "0", "b"),
    (5, "2024-11-03 05:30:00+00", "2024-11-03 01:30:00", 2.0**53 + 2, "1", "c"),
    (6, "2024-11-03 06:30:00+00", "2024-11-03 01:30:00", -0.0, "-1", "d"),
    (7, "2024-03-10 07:00:00+00", "2024-03-10 02:30:00", 5e-7, "0.0000005", "e"),
    (8, None, None, None, None, None),
    (9, "2024-06-15 12:00:00+00", "2024-06-15 12:00:00", 100.0, "100", "plant"),
]


def _pg_dst_table(pgw, name: str) -> str:
    t = f"{SCHEMA}.{name}"
    pgw.execute(
        f"create table {t} (id int primary key, ts timestamptz, nts timestamp, "
        "d date, f float8, x numeric(12,7), s text)"
    )
    with pgw.cursor() as cur:
        cur.executemany(
            f"insert into {t} values (%s, %s, %s, %s::timestamp::date, %s, %s, %s)",
            [(i, ts, nts, nts, f, x, s) for i, ts, nts, f, x, s in DST_ROWS],
        )
    return t


def _duck_dst(tmp_path) -> str:
    path = str(tmp_path / "dst.duckdb")
    con = duckdb_write(path)
    try:
        con.execute("set TimeZone = 'UTC'")
        con.execute(
            "create table t (id int, ts timestamptz, nts timestamp, d date, "
            "f double, x decimal(12,7), s varchar)"
        )
        for i, ts, nts, f, x, s in DST_ROWS:
            con.execute(
                "insert into t values (?, ?::timestamptz, ?::timestamp, "
                "?::timestamp::date, ?, ?::decimal(12,7), ?)",
                [i, ts, nts, nts, f, x, "PLANT 😀" if s == "plant" else s],
            )
    finally:
        con.close()
    return path


@pytest.mark.parametrize(
    "gucs",
    [
        {"TimeZone": "Europe/Zurich"},
        {"TimeZone": "America/New_York", "DateStyle": "SQL,DMY"},
        {"extra_float_digits": "-15", "lc_numeric": "German_Germany.1252"},
        {"DateStyle": "German", "lc_time": "German_Germany.1252"},
        {"default_transaction_isolation": "serializable"},
        {"default_transaction_read_only": "on", "default_transaction_deferrable": "on"},
    ],
    ids=lambda g: ",".join(g),
)
def test_pg_session_defaults_the_dialect_pins_do_not_move_the_verdict(
    pg_url, pgw, tmp_path, gucs
):
    """Role/database defaults the encoding depends on - zone, float digits,
    date style, numeric locale - plus isolation defaults. The dialect pins or
    sidesteps every one: exactly the planted 'plant'/'PLANT' row differs, with
    timestamptz rows straddling DST in two zones rendering as UTC."""
    t = _pg_dst_table(pgw, f"dst_{uuid.uuid4().hex[:8]}_{RUN}")
    a = open_pg(pg_options(pg_url, **gucs))
    result = run_diff(a, open_duckdb(_duck_dst(tmp_path)), t, "main.t")
    assert found(result) == [(9, "different", ("s",))]


def test_pg_two_sessions_in_different_zones_agree_across_dst(pg_url, pgw):
    """Both sides PostgreSQL, sessions defaulting to Zurich and New York: the
    same instants around four DST transitions read identical, and one planted
    instant change one microsecond off is found."""
    t1 = _pg_dst_table(pgw, f"dsta_{RUN}")
    t2 = f"{SCHEMA}.dstb_{RUN}"
    pgw.execute(f"create table {t2} as select * from {t1}")
    pgw.execute(f"update {t2} set ts = ts + interval '1 microsecond' where id = 4")
    a = open_pg(pg_options(pg_url, TimeZone="Europe/Zurich"), side="A")
    b = open_pg(pg_options(pg_url, TimeZone="America/New_York"), side="B")
    result = run_diff(a, b, t1, t2)
    assert found(result) == [(4, "different", ("ts",))]


@pytest.mark.xfail(
    strict=True,
    reason="RT-D5: IntervalStyle / lc_monetary / DateStyle / bytea_output are "
    "not pinned, so unmapped PostgreSQL types render per session (false diff)",
)
@pytest.mark.parametrize(
    "gucs",
    [
        {"IntervalStyle": "iso_8601"},
        {"lc_monetary": "German_Germany.1252"},
        {"DateStyle": "SQL,DMY"},
        {"bytea_output": "escape"},
    ],
    ids=lambda g: ",".join(g),
)
def test_pg_one_table_compared_with_itself_under_another_session_style(pg_url, pgw, gucs):
    """The same PostgreSQL table on both sides, side B's session carrying a
    different output style. interval, money, timestamptz[] and bytea map to no
    logical type and are rendered with `::text`, which follows the session.
    The only planted difference is row 3's text; the styles must not add more.
    (The 'unmapped types' warning says they may differ *across engines*; here
    the engine is the same and the data is literally the same rows.)"""
    t = f"{SCHEMA}.styles_{'_'.join(gucs).lower()}_{RUN}"
    pgw.execute(
        f"create table {t} (id int primary key, iv interval, m money, "
        "arr timestamptz[], by bytea, s text)"
    )
    pgw.execute(
        f"insert into {t} values "
        "(1, '1 day -01:00:00', 1234.56, '{\"2024-10-27 00:30:00+00\"}', '\\x00ff', 'a'), "
        "(2, '-1 mon 2 days', -1.5, null, null, 'b'), (3, null, null, null, null, 'c')"
    )
    t2 = t + "_b"
    pgw.execute(f"create table {t2} as select * from {t}")
    pgw.execute(f"update {t2} set s = 'C' where id = 3")
    a = open_pg(pg_url, side="A")
    b = open_pg(pg_options(pg_url, **gucs), side="B")
    result = run_diff(a, b, t, t2)
    assert found(result) == [(3, "different", ("s",))]


ROWS = 256
#: threshold 8 and factor 4 force a real multi-level walk over 256 keys.
WALK = {"threshold": 8, "bisection_factor": 4}

#: Writes landing after level 1: an insert, a delete, an update, an update
#: that is later reverted, a key moved between top-level buckets, a write in
#: the bucket that already differs, and one in a bucket already proved equal.
MID_WALK_PG = [
    "insert into {t} values (100000, 'inserted mid-walk')",
    "delete from {t} where id = 150",
    "update {t} set v = 'updated mid-walk' where id = 200",
    "update {t} set v = 'flip' where id = 20",
    "update {t} set v = 'v20' where id = 20",
    "update {t} set id = 230230 where id = 30",
    "update {t} set v = 'same bucket' where id = 11",
    "update {t} set v = 'v10-on-b' where id = 10",
]


def test_pg_mid_walk_writes_stay_invisible_to_the_whole_diff(pg_url, pgw, tmp_path):
    """REPEATABLE READ holds across a full multi-level diff, not just two
    key_stats calls: every kind of write committed after level 1 - including
    one that *repairs* the starting difference and one that breaks a bucket
    already proved equal - is invisible, and the verdict is exactly the table
    as it was at the first query."""
    t = f"{SCHEMA}.walk_{RUN}"
    pgw.execute(f"create table {t} (id int primary key, v text)")
    pgw.execute(f"insert into {t} select i, 'v' || i from generate_series(1, {ROWS}) i")
    duck = duck_file(
        tmp_path,
        "create table t (id int, v varchar)",
        f"insert into t select i, 'v' || i from range(1, {ROWS + 1}) r(i)",
        "update t set v = 'v10-on-b' where id = 10",
    )
    a = open_pg(pg_url)

    def write() -> None:
        for sql in MID_WALK_PG:
            pgw.execute(sql.format(t=t))

    fired = after_first_checksum(a, write)
    result = run_diff(a, open_duckdb(duck), t, "main.t", **WALK)
    assert fired, "the mid-walk write never ran"
    assert result.stats.queries > 4, "the walk did not descend past level 1"
    assert found(result) == [(10, "different", ("v",))]
    # Negative control: the writes really landed - a fresh diff sees them.
    fresh = run_diff(open_pg(pg_url), open_duckdb(duck), t, "main.t", **WALK)
    assert {d.key for d in fresh.diffs} == {11, 30, 150, 200, 100000, 230230}


def test_pg_sql_ascii_client_encoding_fails_naming_the_side(pg_url, pgw, tmp_path):
    """PGCLIENTENCODING / a role default of SQL_ASCII. Failing is fine - the
    error must say which side broke (CLAUDE.md section 6)."""
    t = f"{SCHEMA}.enc_{RUN}"
    pgw.execute(f"create table {t} (id int primary key, v text)")
    pgw.execute(f"insert into {t} values (1, 'é'), (2, 'x')")
    duck = duck_file(
        tmp_path,
        "create table t (id int, v varchar)",
        "insert into t values (1, 'e'), (2, 'x')",
    )
    a = open_pg(pg_options(pg_url, client_encoding="SQL_ASCII"))
    try:
        result = run_diff(a, open_duckdb(duck), t, "main.t")
    except ValueError as exc:
        assert "side A" in str(exc)
    else:
        assert found(result) == [(1, "different", ("v",))]


# --------------------------------------------------------------- CLI process

#: The machine's own environment: zone, libpq defaults, Python's encodings
#: and locale. None may move the verdict or break the output. A value of None
#: unsets the variable (the runner may export PYTHONIOENCODING=utf-8).
CLI_ENVIRONMENTS: dict[str, dict[str, str | None]] = {
    "tz-tokyo": {"TZ": "Asia/Tokyo"},
    "pgtz-newyork": {"PGTZ": "America/New_York", "TZ": "Europe/Zurich"},
    "pgdatestyle": {"PGDATESTYLE": "SQL, MDY"},
    "pgoptions-pinned": {
        "PGOPTIONS": "-c TimeZone=Europe/Zurich -c extra_float_digits=-15 "
        "-c lc_numeric=German_Germany.1252"
    },
    "python-cp1252": {"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"},
    "python-default-pipe": {"PYTHONIOENCODING": None, "PYTHONUTF8": None},
    "python-utf8": {"PYTHONUTF8": "1"},
    "locale-de": {"LANG": "de_CH.UTF-8", "LC_ALL": "de_CH.UTF-8", "LANGUAGE": "de"},
}


def _cli_cases():
    # RT-D9 (fixed): human output to a stdout that cannot encode a differing
    # value (cp1252, the Windows default for a redirected stdout before Python
    # 3.15) used to crash with exit 2; the python-cp1252 and
    # python-default-pipe human cases pin that it now reports the difference.
    for env_name in CLI_ENVIRONMENTS:
        for fmt in ("--json", "human"):
            yield pytest.param(env_name, fmt, id=f"{env_name}-{fmt}")


@pytest.mark.parametrize("env_name, fmt", list(_cli_cases()))
def test_cli_verdict_does_not_depend_on_the_machine_environment(
    pg_url, pgw, tmp_path, env_name, fmt
):
    """The CLI in a fresh process under each environment finds exactly the
    planted row (exit 1), and prints it - including side B's emoji, which a
    cp1252 stdout cannot encode as-is."""
    t = _pg_dst_table(pgw, f"cli_{uuid.uuid4().hex[:8]}_{RUN}")
    duck = _duck_dst(tmp_path)
    # Start from a deterministic baseline: the runner's own zone, libpq and
    # Python-encoding variables would otherwise decide the outcome. UTF-8
    # stdout is the baseline so only the encoding cases test encodings.
    drop = ("TZ", "PGTZ", "PGOPTIONS", "PGDATESTYLE", "PYTHONIOENCODING", "PYTHONUTF8")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["PYTHONIOENCODING"] = "utf-8"
    for k, v in CLI_ENVIRONMENTS[env_name].items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    cmd = [
        sys.executable,
        "-m",
        "parity.cli",
        "diff",
        "--a",
        pg_url,
        "--a-table",
        t,
        "--b",
        f"duckdb:///{duck}",
        "--b-table",
        "main.t",
        "--key",
        "id",
    ]
    if fmt == "--json":
        cmd.append("--json")
    proc = subprocess.run(cmd, capture_output=True, env=env, timeout=120, check=False)  # noqa: S603 - our own interpreter and CLI
    assert proc.returncode == 1, proc.stderr.decode("utf-8", "replace")
    if fmt == "--json":
        out = json.loads(proc.stdout.decode("utf-8"))
        assert [(d["key"], d["kind"], d["columns"]) for d in out["differences"]] == [
            (9, "different", ["s"])
        ]
    else:
        assert b"PLANT" in proc.stdout


# --------------------------------------------------------------------- MySQL


@pytest.fixture(scope="module")
def myw(mysql_url):
    """An autocommit writer on the MySQL test database, and cleanup."""
    import mysql.connector

    u = urlparse(mysql_url)
    conn = mysql.connector.connect(
        host=u.hostname,
        port=u.port or 3306,
        user=unquote(u.username or ""),
        password=unquote(u.password or ""),
        database=u.path.lstrip("/"),
        autocommit=True,
    )
    made: list[str] = []

    def run(sql: str, rows=None) -> None:
        cur = conn.cursor()
        try:
            if rows is None:
                cur.execute(sql)
            else:
                cur.executemany(sql, rows)
        finally:
            cur.close()

    def table(base: str) -> str:
        name = f"rt_live_{base}_{RUN}"
        made.append(name)
        return name

    yield run, table
    for name in made:
        run(f"drop table if exists `{name}`")
    conn.close()


def _mysql_walk_table(myw, base: str, engine: str) -> str:
    run, table = myw
    t = table(base)
    run(
        f"create table `{t}` (id int primary key, v varchar(40)) "
        f"engine={engine} charset utf8mb4"
    )
    run(f"insert into `{t}` values (%s, %s)", [(i, f"v{i}") for i in range(1, ROWS + 1)])
    return t


def _duck_walk(tmp_path) -> str:
    return duck_file(
        tmp_path,
        "create table t (id int, v varchar)",
        f"insert into t select i, 'v' || i from range(1, {ROWS + 1}) r(i)",
        "update t set v = 'v10-on-b' where id = 10",
    )


MID_WALK_MYSQL = [s.replace("{t}", "`{t}`") for s in MID_WALK_PG]


def test_mysql_innodb_mid_walk_writes_stay_invisible(mysql_url, myw, tmp_path):
    """InnoDB's REPEATABLE READ snapshot holds across the whole multi-level
    walk: the same battery of mid-walk writes as on PostgreSQL is invisible."""
    run, _ = myw
    t = _mysql_walk_table(myw, "innodb", "InnoDB")
    a = open_mysql(mysql_url)
    fired = after_first_checksum(
        a, lambda: [run(sql.format(t=t)) for sql in MID_WALK_MYSQL]
    )
    result = run_diff(a, open_duckdb(_duck_walk(tmp_path)), t, "main.t", **WALK)
    assert fired
    assert found(result) == [(10, "different", ("v",))]


@pytest.mark.parametrize("engine", ["MyISAM", "MEMORY"])
def test_mysql_non_transactional_table_changed_mid_walk(mysql_url, myw, tmp_path, engine):
    """The MySQL dialect's single snapshot is InnoDB's. A legacy MyISAM (or
    MEMORY) table ignores it: each query sees the table as it is now. At the
    start, key 10 differs. After level 1 a writer repairs key 10 and breaks
    key 200, whose bucket level 1 already proved equal. The tables differed at
    every instant, yet the walk descends only into the repaired bucket and
    reports identical with no warning. A correct tool reports a difference, or
    at least says the walk was not one snapshot (as SQL Server's dialect does)."""
    run, _ = myw
    t = _mysql_walk_table(myw, engine.lower(), engine)
    a = open_mysql(mysql_url)

    def write() -> None:
        run(f"update `{t}` set v = 'v10-on-b' where id = 10")
        run(f"update `{t}` set v = 'broken after level 1' where id = 200")

    fired = after_first_checksum(a, write)
    result = run_diff(a, open_duckdb(_duck_walk(tmp_path)), t, "main.t", **WALK)
    assert fired
    warned = any("snapshot" in w.lower() for w in result.warnings)
    assert warned or not result.identical


def _mysql_with_init_connect(monkeypatch, statement: str) -> None:
    """Make every new MySQL connection run `statement` first, exactly where a
    server's `init_connect` runs: after the handshake, *before* the client's
    first command - and so before mysql-connector's own post-connection
    `SET NAMES` / autocommit statements and the dialect's session pins.

    (An earlier version ran it after `connect()` returned, i.e. after the
    connector's `SET NAMES utf8mb4`. That modelled no real server setting and
    made `init_connect='SET NAMES latin1'` look like a bug: the connector
    always re-issues `SET NAMES` and overrides it.) `init_connect` itself is
    global and the server is shared, so it is reproduced client-side."""
    from mysql.connector.abstracts import MySQLConnectionAbstract

    real = MySQLConnectionAbstract._post_connection

    def post_connection(self) -> None:
        self._execute_query(statement)
        real(self)

    monkeypatch.setattr(MySQLConnectionAbstract, "_post_connection", post_connection)


@pytest.mark.parametrize(
    "statement",
    [
        "set names latin1",
        "set character_set_results = latin1",
        "set names utf8mb3",
        "set collation_connection = utf8mb4_general_ci",
    ],
)
def test_mysql_connection_charset_from_init_connect(
    mysql_url, myw, monkeypatch, statement
):
    """`init_connect = 'SET NAMES latin1'` is a classic legacy-MySQL setting -
    exactly the system a migration retires. Had it stuck, `cast(c as char)`
    would convert to latin1 server-side (hash over '?') and the result set
    would arrive in latin1, so 😀 on side A and 😁 on side B would compare
    equal. It does not stick: mysql-connector issues its own
    `SET NAMES utf8mb4` after `init_connect` has run. (Formerly filed as
    RT-D4 against a wrapper that ran the statement *after* the connector's;
    withdrawn - no server setting reaches that point. The dialect still
    relies on the driver for this rather than pinning it itself.)"""
    run, table = myw
    tag = uuid.uuid4().hex[:6]
    ta, tb = table(f"cs_a_{tag}"), table(f"cs_b_{tag}")
    for t, v in ((ta, "x\U0001f600"), (tb, "x\U0001f601")):
        run(f"create table `{t}` (id int primary key, v varchar(20)) charset utf8mb4")
        run(f"insert into `{t}` values (%s, %s)", [(1, v), (2, "ok")])
    _mysql_with_init_connect(monkeypatch, statement)
    result = run_diff(open_mysql(mysql_url, "A"), open_mysql(mysql_url, "B"), ta, tb)
    assert found(result) == [(1, "different", ("v",))]
    assert result.diffs[0].values_a == {"v": "x\U0001f600"}


@pytest.mark.parametrize(
    "statement",
    [
        "set session sql_mode = 'ANSI,NO_BACKSLASH_ESCAPES'",
        "set session sql_mode = 'TRADITIONAL,HIGH_NOT_PRECEDENCE,NO_UNSIGNED_SUBTRACTION'",
        "set session sql_mode = ''",
        "set session time_zone = '+09:00'",
        "set session div_precision_increment = 0",
        "set session transaction isolation level read committed",
        "set session collation_connection = utf8mb4_bin",
    ],
)
def test_mysql_server_defaults_that_do_not_move_the_verdict(
    mysql_url, myw, tmp_path, monkeypatch, statement
):
    """Server-default sql_mode variants (ANSI quotes, pipes-as-concat,
    backslash escapes off), zone, division precision, a READ COMMITTED
    default and a binary connection collation: the canonical text holds and
    exactly the planted rows differ, including a TIMESTAMP read as UTC."""
    run, table = myw
    t = table(f"modes_{uuid.uuid4().hex[:6]}")
    run(
        f"create table `{t}` (id int primary key, ts timestamp(6) null, "
        "dt datetime(6), x decimal(12,7), f double, s varchar(40), "
        "b boolean) charset utf8mb4"
    )
    run("set time_zone = '+00:00'")
    run(
        f"insert into `{t}` values (%s, %s, %s, %s, %s, %s, %s)",
        [
            (
                1,
                "2024-10-27 01:30:00.5",
                "2024-10-27 02:30:00",
                "1.1234567",
                0.1,
                "a\\b",
                1,
            ),
            (2, None, None, None, None, None, None),
            (
                3,
                "2024-03-31 01:00:00",
                "2024-03-31 02:00:00",
                "-0.0000005",
                1 / 3,
                "é😀",
                0,
            ),
            (4, "2024-06-15 12:00:00", "2024-06-15 12:00:00", "2", 2.5, "plant", 1),
        ],
    )
    duck = duck_file(
        tmp_path,
        "create table t (id int, ts timestamp, dt timestamp, x decimal(12,7), "
        "f double, s varchar, b tinyint)",
        "insert into t values "
        "(1, '2024-10-27 01:30:00.5', '2024-10-27 02:30:00', 1.1234567, 0.1, 'a\\b', 1), "
        "(2, null, null, null, null, null, null), "
        "(3, '2024-03-31 01:00:00', '2024-03-31 02:00:00', -0.0000005, 1/3, 'é😀', 0), "
        "(4, '2024-06-15 12:00:00', '2024-06-15 12:00:00', 2, 2.5, 'PLANT', 1)",
    )
    _mysql_with_init_connect(monkeypatch, statement)
    result = run_diff(open_mysql(mysql_url), open_duckdb(duck), t, "main.t")
    assert found(result) == [(4, "different", ("s",))]


# ---------------------------------------------------------------- SQL Server


def _mssql_db_url(url: str, database: str, user: str = "", password: str = "") -> str:
    u = urlparse(url)
    if user:
        host = f"{u.hostname}:{u.port}" if u.port else str(u.hostname)
        u = u._replace(netloc=f"{quote(user, safe='')}:{quote(password, safe='')}@{host}")
    return urlunparse(u._replace(path="/" + database))


class MssqlAdmin:
    """Autocommit sa connections per database, for fixtures."""

    def __init__(self, url: str) -> None:
        self.url = url

    def run(self, database: str, sql: str):
        import mssql_python

        from parity.dialects.mssql_dialect import parse_mssql_url

        odbc, _ = parse_mssql_url(_mssql_db_url(self.url, database))
        conn = mssql_python.connect(odbc, autocommit=True)
        try:
            cur = conn.cursor()
            cur.execute(sql)
            rows = [tuple(r) for r in cur.fetchall()] if cur.description else []
            cur.close()
            return rows
        finally:
            conn.close()

    def drop_database(self, name: str) -> None:
        self.run(
            "master",
            f"if db_id('{name}') is not null begin alter database [{name}] "
            f"set single_user with rollback immediate; drop database [{name}] end",
        )


MSSQL_DB = f"rt_live_db_{RUN}"
MSSQL_RC_DB = f"rt_live_rc_{RUN}"
MSSQL_LOGIN = f"rt_live_login_{RUN}"


@pytest.fixture(scope="module")
def mss(mssql_url):
    """Two databases (snapshot allowed / not) with hostile database-level ANSI
    options, and a login whose default language is German (DATEFIRST 1, dmy)."""
    adm = MssqlAdmin(mssql_url)
    password = "Rt!" + secrets.token_hex(16)
    for db, snapshot in ((MSSQL_DB, "on"), (MSSQL_RC_DB, "off")):
        adm.drop_database(db)
        adm.run("master", f"create database [{db}]")
        for opt in (
            f"allow_snapshot_isolation {snapshot}",
            "concat_null_yields_null off",
            "ansi_nulls off",
            "ansi_padding off",
            "ansi_warnings off",
            "arithabort off",
            "quoted_identifier off",
        ):
            adm.run("master", f"alter database [{db}] set {opt}")
    adm.run(
        "master", f"if suser_id('{MSSQL_LOGIN}') is not null drop login [{MSSQL_LOGIN}]"
    )
    adm.run(
        "master",
        f"create login [{MSSQL_LOGIN}] with password = '{password}', "
        f"default_database = [{MSSQL_DB}], default_language = Deutsch",
    )
    for db in (MSSQL_DB, MSSQL_RC_DB):
        adm.run(db, f"create user [{MSSQL_LOGIN}] for login [{MSSQL_LOGIN}]")
        adm.run(db, f"alter role db_datareader add member [{MSSQL_LOGIN}]")
    yield adm, lambda db: _mssql_db_url(mssql_url, db, MSSQL_LOGIN, password)
    for db in (MSSQL_DB, MSSQL_RC_DB):
        adm.drop_database(db)
    adm.run(
        "master", f"if suser_id('{MSSQL_LOGIN}') is not null drop login [{MSSQL_LOGIN}]"
    )


MSSQL_VALUES = (
    "(1, '20240229', '20241027 02:30:00.003', '20240331 02:30:00.123456', "
    "1.12345678, 0.1, N'a', 1), "
    "(2, null, null, null, null, null, null, null), "
    "(3, '00010101', '17530101', '99991231 23:59:59.999999', -0.0000005, 1e15, N'b', 0)"
)
DUCK_VALUES = (
    "(1, '2024-02-29', '2024-10-27 02:30:00.003', '2024-03-31 02:30:00.123456', "
    "1.12345678, 0.1, 'a', true), "
    "(2, null, null, null, null, null, null, null), "
    "(3, '0001-01-01', '1753-01-01', '9999-12-31 23:59:59.999999', -0.0000005, 1e15, "
    "'B', false)"
)


def _mssql_types_table(adm, db: str, name: str) -> str:
    adm.run(
        db,
        f"create table dbo.{name} (id bigint primary key, d date, dt datetime, "
        "dt2 datetime2(6), x decimal(12,8), f float, s nvarchar(20), b bit)",
    )
    # Literals are language-neutral (yyyymmdd), so the German login reads
    # them the same; the session that matters is the dialect's.
    adm.run(db, f"insert into dbo.{name} values {MSSQL_VALUES}")
    return name


def _duck_types(tmp_path) -> str:
    return duck_file(
        tmp_path,
        "create table t (id bigint, d date, dt timestamp, dt2 timestamp, "
        "x decimal(12,8), f double, s varchar, b boolean)",
        f"insert into t values {DUCK_VALUES}",
    )


def test_mssql_login_language_and_database_ansi_options_do_not_move_the_verdict(
    mss, tmp_path
):
    """A German-language login (DATEFIRST 1, dmy) in a database whose own
    defaults turn CONCAT_NULL_YIELDS_NULL, ANSI_NULLS, ANSI_PADDING,
    ANSI_WARNINGS, ARITHABORT and QUOTED_IDENTIFIER off. The driver resets
    the ANSI set per session and every rendering uses explicit styles, so NULL
    datetimes still render the sentinel (not `'000'`) and only the planted
    'b'/'B' row differs."""
    adm, url = mss
    t = _mssql_types_table(adm, MSSQL_DB, f"lang_{RUN}")
    a = open_mssql(url(MSSQL_DB))
    assert a.query("select @@language, @@datefirst")[0] == ("Deutsch", 1)
    result = run_diff(a, open_duckdb(_duck_types(tmp_path)), t, "main.t")
    assert found(result) == [(3, "different", ("s",))]


@pytest.mark.xfail(
    strict=True,
    reason="RT-D6: a database with NUMERIC_ROUNDABORT ON makes every DECIMAL/FLOAT "
    "rounding raise; the dialect does not pin it OFF and the error names no side",
)
def test_mssql_numeric_roundabort_database_still_diffs(mss, tmp_path):
    """NUMERIC_ROUNDABORT is a database option the ODBC driver does *not*
    reset. On, every `cast(x as decimal(38,6))` that drops a digit raises
    error 8115 - so any DECIMAL with more than six places, and the FLOAT
    rendering, cannot be compared at all."""
    adm, url = mss
    t = _mssql_types_table(adm, MSSQL_DB, f"roundabort_{RUN}")
    adm.run("master", f"alter database [{MSSQL_DB}] set numeric_roundabort on")
    try:
        try:
            result = run_diff(
                open_mssql(url(MSSQL_DB)), open_duckdb(_duck_types(tmp_path)), t, "main.t"
            )
        except Exception as exc:
            assert "side A" in str(exc), f"error names no side: {exc}"
            raise
        assert found(result) == [(3, "different", ("s",))]
    finally:
        adm.run("master", f"alter database [{MSSQL_DB}] set numeric_roundabort off")


def _mssql_walk_table(adm, db: str, name: str, values_on_b: bool = False) -> str:
    adm.run(db, f"create table dbo.{name} (id int primary key, v nvarchar(40))")
    adm.run(
        db,
        f"insert into dbo.{name} select top ({ROWS}) "
        "row_number() over (order by (select null)), null from sys.all_objects",
    )
    adm.run(db, f"update dbo.{name} set v = N'v' + cast(id as nvarchar(10))")
    return name


MID_WALK_MSSQL = [s.replace("{t}", "dbo.{t}").replace("'", "N'") for s in MID_WALK_PG]


def test_mssql_snapshot_mid_walk_writes_stay_invisible(mss, tmp_path):
    """With ALLOW_SNAPSHOT_ISOLATION ON the whole multi-level diff - not just
    two key_stats calls - reads one snapshot, as a read-only login."""
    adm, url = mss
    t = _mssql_walk_table(adm, MSSQL_DB, f"walk_{RUN}")
    a = open_mssql(url(MSSQL_DB))
    assert a.snapshot
    fired = after_first_checksum(
        a, lambda: [adm.run(MSSQL_DB, s.format(t=t)) for s in MID_WALK_MSSQL]
    )
    result = run_diff(a, open_duckdb(_duck_walk(tmp_path)), f"dbo.{t}", "main.t", **WALK)
    assert fired
    assert found(result) == [(10, "different", ("v",))]
    assert not any("READ COMMITTED" in w for w in result.warnings)


def test_mssql_read_committed_false_match_is_at_least_warned(mss, tmp_path):
    """Without snapshot isolation the same repair-one-break-another write reads
    identical - a verdict true at no instant. That is the documented gap; what
    must hold is that the run says so, naming the side."""
    adm, url = mss
    t = _mssql_walk_table(adm, MSSQL_RC_DB, f"rcwalk_{RUN}")
    a = open_mssql(url(MSSQL_RC_DB))

    def write() -> None:
        adm.run(MSSQL_RC_DB, f"update dbo.{t} set v = N'v10-on-b' where id = 10")
        adm.run(MSSQL_RC_DB, f"update dbo.{t} set v = N'broken' where id = 200")

    fired = after_first_checksum(a, write)
    result = run_diff(a, open_duckdb(_duck_walk(tmp_path)), f"dbo.{t}", "main.t", **WALK)
    assert fired
    assert result.identical  # the documented false match...
    assert any("side A (mssql)" in w and "READ COMMITTED" in w for w in result.warnings)


# -------------------------------------------------------------------- DuckDB

_HOLD_WRITER = textwrap.dedent(
    """
    import sys, duckdb
    con = duckdb.connect(sys.argv[1])
    con.execute("update t set v = 'held' where id = 1")
    print("ready", flush=True)
    sys.stdin.readline()
    con.close()
    """
)

_TRY_WRITE = textwrap.dedent(
    """
    import sys, duckdb
    try:
        con = duckdb.connect(sys.argv[1])
        con.execute("update t set v = 'written mid-diff' where id = 200")
        con.close()
        print("WROTE")
    except Exception as exc:
        print("REFUSED", type(exc).__name__)
    """
)


def test_duckdb_file_held_open_by_a_writer_process_is_refused_loudly(tmp_path):
    """Another process holds the file read-write: the dialect must refuse to
    open it (loudly, naming the side), never read a half-written state."""
    path = duck_file(
        tmp_path, "create table t (id int, v varchar)", "insert into t values (1, 'x')"
    )
    proc = subprocess.Popen(  # noqa: S603 - our own interpreter, fixed script
        [sys.executable, "-c", _HOLD_WRITER, path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
        with pytest.raises(ValueError, match=r"side B"):
            get_dialect(f"duckdb:///{path}", side="B")
    finally:
        proc.communicate("\n", timeout=30)


def test_duckdb_file_cannot_be_modified_by_another_process_mid_diff(
    pg_url, pgw, tmp_path
):
    """The reverse: the dialect holds the file read-only, and a writer process
    started between bisection levels is locked out - so the DuckDB side is one
    snapshot by construction, and the verdict is the starting state."""
    t = f"{SCHEMA}.duckwalk_{RUN}"
    pgw.execute(f"create table {t} (id int primary key, v text)")
    pgw.execute(f"insert into {t} select i, 'v' || i from generate_series(1, {ROWS}) i")
    path = _duck_walk(tmp_path)
    b = open_duckdb(path)
    outcome: list[str] = []

    def write() -> None:
        proc = subprocess.run(  # noqa: S603 - our own interpreter, fixed script
            [sys.executable, "-c", _TRY_WRITE, path],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        outcome.append(proc.stdout.strip())

    after_first_checksum(b, write)
    result = run_diff(open_pg(pg_url), b, t, "main.t", **WALK)
    assert outcome and outcome[0].startswith("REFUSED"), outcome
    assert found(result) == [(10, "different", ("v",))]


# ------------------------------------------------------- BigQuery, Snowflake


def test_bigquery_view_run_says_it_is_not_one_snapshot(bigquery_url):
    """CLAUDE.md section 8: an approximation the docs admit to must reach the
    output. The README says views are not pinned; SQL Server's dialect turns
    the same gap into a warning, BigQuery's says nothing."""
    from google.cloud import bigquery

    from parity.dialects.bigquery_dialect import parse_bigquery_url

    project, dataset, location = parse_bigquery_url(bigquery_url)
    client = bigquery.Client(project=project, location=location)
    base, view = f"rt_live_base_{RUN}", f"rt_live_view_{RUN}"
    ds = f"`{project}`.`{dataset}`"
    try:
        client.query_and_wait(f"create table {ds}.`{base}` as select 1 as id, 'x' as v")
        client.query_and_wait(f"create view {ds}.`{view}` as select * from {ds}.`{base}`")
        a = get_dialect(bigquery_url, side="A")
        try:
            cols = a.columns(view)
            notes = a.caveats(cols)
        finally:
            a.close()
        assert any("view" in n.lower() or "snapshot" in n.lower() for n in notes), notes
    finally:
        for name in (view, base):
            client.delete_table(f"{project}.{dataset}.{name}", not_found_ok=True)
        client.close()


def test_bigquery_view_caveat_offline():
    """RT-D7, offline: a view seen by `columns()` is named in a caveat, and a
    base table - pinned with time travel - raises none."""
    from parity.dialects.bigquery_dialect import BigQueryDialect

    class Stub(BigQueryDialect):
        kind = "VIEW"

        def query(self, sql):  # type: ignore[override]
            """One INFORMATION_SCHEMA row of the configured table type."""
            return [("id", "INT64", self.kind, "2026-11-03 00:00:00.000000+00")]

    base = Stub(side="A")
    base.project, base.kind = "p", "BASE TABLE"
    assert base.caveats(base.columns("ds.t")) == []
    view = Stub(side="A")
    view.project = "p"
    (note,) = view.caveats(view.columns("ds.v"))
    assert "p.ds.v" in note and "view" in note and "snapshot" in note


def test_snowflake_run_says_it_is_not_one_snapshot():
    """Offline: the dialect's own docstring admits the gap; the output must too."""
    from parity.dialects.snowflake_dialect import SnowflakeDialect

    notes = SnowflakeDialect(side="A").caveats([])
    assert any("snapshot" in n.lower() or "read committed" in n.lower() for n in notes)


@pytest.mark.parametrize(
    "statement",
    [
        # RT-D10 (fixed): the dialect pins its own session sql_mode.
        pytest.param("set session sql_mode = 'PAD_CHAR_TO_FULL_LENGTH'", id="pad_char"),
        pytest.param("do 0", id="control"),
    ],
)
def test_mysql_char_padding_mode(mysql_url, myw, tmp_path, monkeypatch, statement):
    """PAD_CHAR_TO_FULL_LENGTH (deprecated, still in 8.0) makes CHAR values
    come back padded. Planted: row 2 differs; row 1 must not."""
    run, table = myw
    t = table(f"pad_{uuid.uuid4().hex[:6]}")
    run(f"create table `{t}` (id int primary key, c char(5)) charset utf8mb4")
    run(f"insert into `{t}` values (1, 'ab'), (2, 'x')")
    duck = duck_file(
        tmp_path,
        "create table t (id int, c varchar)",
        "insert into t values (1, 'ab'), (2, 'y')",
    )
    _mysql_with_init_connect(monkeypatch, statement)
    result = run_diff(open_mysql(mysql_url), open_duckdb(duck), t, "main.t")
    assert found(result) == [(2, "different", ("c",))]


# ------------------------------------------------- extended coverage (resumed)


def test_mysql_case_sensitive_text_key_is_not_called_duplicate(mysql_url, myw):
    """`utf8mb4_bin` key columns are ordinary - case-sensitive codes, base62
    ids, short-URL slugs. `cast(k as char)` re-collates to the connection's
    `utf8mb4_0900_ai_ci`, so `count(distinct ...)` merges 'a' and 'A' and the
    diff refuses a perfectly unique key. Planted: key 'b' differs."""
    run, table = myw
    tag = uuid.uuid4().hex[:6]
    ta, tb = table(f"ck_a_{tag}"), table(f"ck_b_{tag}")
    for t, bv in ((ta, 3), (tb, 99)):
        run(
            f"create table `{t}` (k varchar(10) collate utf8mb4_bin primary key, "
            "v int) charset utf8mb4"
        )
        run(f"insert into `{t}` values ('a', 1), ('A', 2), ('b', {bv})")
    result = run_diff(
        open_mysql(mysql_url, "A"), open_mysql(mysql_url, "B"), ta, tb, key="k"
    )
    assert found(result) == [("b", "different", ("v",))]


@pytest.mark.xfail(
    strict=True,
    reason="RT-D11: count(distinct) of a text key follows a nondeterministic "
    "PostgreSQL collation, so 'a'/'A' are refused as a duplicate key",
)
def test_pg_nondeterministic_collation_text_key_is_not_called_duplicate(pgw, pg_url):
    """A case-insensitive ICU collation (deterministic = false) - the standard
    PostgreSQL answer to case-insensitive email/user-name columns. The values
    'a' and 'A' are distinct rows; the comparison must still run."""
    coll = f"{SCHEMA}.ci_{RUN}"
    pgw.execute(
        f"create collation if not exists {coll} "
        "(provider = icu, locale = 'und-u-ks-level2', deterministic = false)"
    )
    ta, tb = f"{SCHEMA}.ck_a_{RUN}", f"{SCHEMA}.ck_b_{RUN}"
    for t, bv in ((ta, 3), (tb, 99)):
        pgw.execute(f"create table {t} (k text collate {coll}, v int)")
        pgw.execute(f"insert into {t} values ('a', 1), ('A', 2), ('b', {bv})")
    result = run_diff(open_pg(pg_url, "A"), open_pg(pg_url, "B"), ta, tb, key="k")
    assert found(result) == [("b", "different", ("v",))]


def test_pg_query_error_names_the_side(pg_url, pgw):
    """CLAUDE.md section 6: errors must name the side and the table. Only
    connect-time and a few dialect-specific errors do; any server error from a
    checksum or fetch query - statement_timeout (a common role default on
    production PostgreSQL), lock_timeout, a cancelled query, a dropped
    connection - arrives bare. PostgreSQL-to-PostgreSQL migrations make it
    impossible to tell which server said it."""
    t = f"{SCHEMA}.big_{RUN}"
    pgw.execute(
        f"create table {t} as select i as id, md5(i::text) as v "
        "from generate_series(1, 300000) i"
    )
    b = open_pg(pg_options(pg_url, statement_timeout="1"), "B")
    with pytest.raises(Exception) as info:
        run_diff(open_pg(pg_url, "A"), b, t, t)
    assert "side B" in str(info.value), f"{type(info.value).__name__}: {info.value}"


@pytest.mark.xfail(
    strict=True,
    reason="RT-D13: the held REPEATABLE READ transaction is killed by a role "
    "default idle_in_transaction_session_timeout / transaction_timeout while "
    "the other side's query runs; the dialect does not pin either for its session",
)
@pytest.mark.parametrize(
    "gucs",
    [
        {"idle_in_transaction_session_timeout": "1000"},
        {"transaction_timeout": "1500"},
    ],
    ids=lambda g: ",".join(g),
)
def test_pg_snapshot_survives_a_slow_other_side(pg_url, pgw, tmp_path, gucs):
    """The PostgreSQL side's snapshot transaction sits idle whenever the other
    side is slower - which, against a cloud warehouse, is every level. An
    `idle_in_transaction_session_timeout` of a minute is a common production
    role/database default (it protects vacuum); PG 17's `transaction_timeout`
    likewise. Both are session-settable by any user, so the dialect could pin
    them to 0 for its read-only session, as it pins TimeZone. Here side B
    takes 2.5 s after level 1 (simulated); the diff must still finish."""
    import time

    t = f"{SCHEMA}.idle_{next(iter(gucs))[:12]}_{RUN}"
    pgw.execute(f"create table {t} (id int primary key, v text)")
    pgw.execute(f"insert into {t} select i, 'v' || i from generate_series(1, {ROWS}) i")
    b = open_duckdb(_duck_walk(tmp_path))
    after_first_checksum(b, lambda: time.sleep(2.5))
    result = run_diff(open_pg(pg_options(pg_url, **gucs)), b, t, "main.t", **WALK)
    assert found(result) == [(10, "different", ("v",))]


@pytest.mark.xfail(
    strict=True,
    reason="RT-D14: built-ins are called unqualified, so an exact-signature "
    "function on search_path (here to_char(date, text)) replaces pg_catalog's "
    "in the canonical text (false match)",
)
def test_pg_user_function_on_search_path_cannot_replace_a_builtin(pg_url, pgw, tmp_path):
    """pg_catalog has no `to_char(date, text)`; the DATE rendering resolves to
    `to_char(timestamptz, text)` through an implicit cast. Any schema on the
    search_path - `public` included, with the stock `"$user", public`, and
    *after* public too - that holds an exact `to_char(date, text)` wins.
    Compatibility layers and hand-written migration helpers define such
    overloads on exactly the systems a migration compares. This one renders
    the month only. Against another engine that is a false diff on every row;
    PostgreSQL against PostgreSQL with the helper on both (two tables in one
    database, or a PG-to-PG move that carried its helpers along) it is a
    false match: a changed day is invisible. Planted: row 1's day."""
    pgw.execute(
        f"create or replace function {SHADOW}.to_char(date, text) returns text "
        "language sql immutable as $$ select pg_catalog.to_char($1, 'YYYY-MM') $$"
    )
    t1, t2 = f"{SCHEMA}.fnd_a_{RUN}", f"{SCHEMA}.fnd_b_{RUN}"
    for t, day in ((t1, 15), (t2, 16)):
        pgw.execute(f"create table {t} (id int primary key, d date)")
        pgw.execute(f"insert into {t} values (1, '2024-01-{day}'), (2, '2024-02-01')")
    url = pg_options(pg_url, search_path=f"public,{SHADOW}")
    try:
        result = run_diff(open_pg(url, "A"), open_pg(url, "B"), t1, t2)
    finally:
        pgw.execute(f"drop function if exists {SHADOW}.to_char(date, text)")
    assert found(result) == [(1, "different", ("d",))]


def test_pg_truncate_mid_walk_is_blocked_by_the_snapshot_lock(pg_url, pgw, tmp_path):
    """TRUNCATE is not MVCC-safe: a REPEATABLE READ snapshot taken before it
    commits sees the table *empty* afterwards. It cannot commit mid-walk,
    because the diff's transaction holds ACCESS SHARE from the first checksum
    to the end - so a TRUNCATE-and-reload ETL job waits, and the verdict is
    the starting state. A lock_timeout keeps the test from hanging."""
    import psycopg

    t = f"{SCHEMA}.trunc_{RUN}"
    pgw.execute(f"create table {t} (id int primary key, v text)")
    pgw.execute(f"insert into {t} select i, 'v' || i from generate_series(1, {ROWS}) i")
    a = open_pg(pg_url)
    outcome: list[str] = []

    def write() -> None:
        with psycopg.connect(pg_url, autocommit=True) as w:
            w.execute("set lock_timeout = '500ms'")
            try:
                w.execute(f"truncate {t}")
                outcome.append("TRUNCATED")
            except psycopg.errors.LockNotAvailable:
                outcome.append("BLOCKED")

    after_first_checksum(a, write)
    result = run_diff(a, open_duckdb(_duck_walk(tmp_path)), t, "main.t", **WALK)
    assert outcome == ["BLOCKED"]
    assert found(result) == [(10, "different", ("v",))]


def _mysql_writer(mysql_url: str):
    import mysql.connector

    u = urlparse(mysql_url)
    return mysql.connector.connect(
        host=u.hostname,
        port=u.port or 3306,
        user=unquote(u.username or ""),
        password=unquote(u.password or ""),
        database=u.path.lstrip("/"),
        autocommit=True,
    )


def test_mysql_innodb_truncate_mid_walk_is_blocked_by_the_metadata_lock(
    mysql_url, myw, tmp_path
):
    """The MySQL twin: the open transaction's metadata lock makes a mid-walk
    TRUNCATE (and any ALTER) wait; lock_wait_timeout keeps the test bounded."""
    import mysql.connector

    t = _mysql_walk_table(myw, "trunc", "InnoDB")
    a = open_mysql(mysql_url)
    outcome: list[str] = []

    def write() -> None:
        w = _mysql_writer(mysql_url)
        cur = w.cursor()
        try:
            cur.execute("set session lock_wait_timeout = 1")
            try:
                cur.execute(f"truncate table `{t}`")
                outcome.append("TRUNCATED")
            except mysql.connector.Error as exc:
                outcome.append("BLOCKED" if exc.errno == 1205 else f"ERR {exc.errno}")
        finally:
            cur.close()
            w.close()

    after_first_checksum(a, write)
    result = run_diff(a, open_duckdb(_duck_walk(tmp_path)), t, "main.t", **WALK)
    assert outcome == ["BLOCKED"]
    assert found(result) == [(10, "different", ("v",))]


@pytest.mark.parametrize("kind", ["view", "partitioned"])
def test_mysql_innodb_view_and_partitioned_table_mid_walk(mysql_url, myw, tmp_path, kind):
    """The InnoDB snapshot is per transaction, not per table: a view over an
    InnoDB table and a partitioned InnoDB table both hold it across the walk -
    repairing the starting difference and breaking a bucket already proved
    equal after level 1 are both invisible."""
    run, table = myw
    base = _mysql_walk_table(myw, f"{kind[:4]}_base", "InnoDB")
    target = base
    if kind == "view":
        target = table("v_" + uuid.uuid4().hex[:6])
        run(f"create view `{target}` as select id, v from `{base}`")
    else:
        run(f"alter table `{base}` partition by hash(id) partitions 4")
    a = open_mysql(mysql_url)

    def write() -> None:
        run(f"update `{base}` set v = 'v10-on-b' where id = 10")
        run(f"update `{base}` set v = 'broken after level 1' where id = 200")

    fired = after_first_checksum(a, write)
    try:
        result = run_diff(a, open_duckdb(_duck_walk(tmp_path)), target, "main.t", **WALK)
    finally:
        if kind == "view":
            run(f"drop view if exists `{target}`")
    assert fired
    assert found(result) == [(10, "different", ("v",))]
