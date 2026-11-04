"""Red team: server, database, role and session settings the dialects do not pin.

The previous campaign (RT-D, `test_live.py`) attacked session defaults by
planting them the way a backend starts with them. This one goes after what a
DBA sets *server-wide* or per database - GLOBAL variables, `init_connect`,
`sp_configure`, database encodings and compatibility levels, user-level
Snowflake parameters, row-level security - plus a sweep of every
user-settable PostgreSQL parameter.

Confirmed bugs are `xfail(strict=True)` with an `RT-G<n>` id, asserting the
behaviour a trustworthy parity tool must show; everything that was attacked
and held is an ordinary passing test. Server-wide settings are only ever
changed on the disposable Docker containers of `rtg_servers.py` (and reset in
a `finally`); on the shared servers only objects named `rtg_*` / `parity_rtg`
are created, and session-level settings are planted client-side.
"""

from __future__ import annotations

import uuid
from urllib.parse import quote

import pytest
from conftest import open_duckdb, open_mssql, open_mysql, open_pg

from parity.dialects.base import get_dialect

from .rtg_servers import (
    MSSQL_ADMIN_URL,
    MYSQL_LATIN1_ROOT_URL,
    MYSQL_ROOT_URL,
    MYSQL_USER_URL,
    PG_ADMIN_URL,
    duck_file,
    found,
    mssql_drop_database,
    mssql_exec,
    mssql_url,
    mysql_connect,
    mysql_run,
    pg_connect,
    pg_with_db,
    run_diff,
)

RUN = uuid.uuid4().hex[:6]


def pg_options(url: str, **gucs: str) -> str:
    """`url` with libpq `options=-c k=v ...`: a backend-start session default,
    exactly what `ALTER ROLE/DATABASE ... SET` and PGOPTIONS feed."""
    opts = " ".join(f"-c {k}={v.replace(' ', chr(92) + ' ')}" for k, v in gucs.items())
    return url + ("&" if "?" in url else "?") + "options=" + quote(opts, safe="")


# =================================================================== MySQL


def _mysql_wide_tables(conn, a: str, b: str) -> None:
    """Two 200-row tables (id, a, b, c) that differ only in column `c` of row 150."""
    rows = ", ".join(f"({i}, 'a{i}', 'b{i}', 'c{i}')" for i in range(1, 201))
    for t in (a, b):
        mysql_run(
            conn,
            f"drop table if exists `{t}`",
            f"create table `{t}` (id int primary key, a varchar(20), b varchar(20), "
            "c varchar(20)) charset utf8mb4",
            f"insert into `{t}` values {rows}",
        )
    mysql_run(conn, f"update `{b}` set c = 'PLANT' where id = 150")


def _init_connect_hook(monkeypatch, statement: str) -> None:
    """Run `statement` on every new MySQL connection where `init_connect`
    runs - after the handshake, before mysql-connector's own `SET NAMES` and
    the dialect's session statements. Client-side, so the shared server is
    untouched; the Docker tests below set the real `init_connect`."""
    from mysql.connector.abstracts import MySQLConnectionAbstract

    real = MySQLConnectionAbstract._post_connection

    def post_connection(self) -> None:
        """Run `statement` first, then the connector's real setup."""
        self._execute_query(statement)
        real(self)

    monkeypatch.setattr(MySQLConnectionAbstract, "_post_connection", post_connection)


def test_rt_g1_mysql_sql_select_limit_session_default(mysql_url, monkeypatch):
    """`sql_select_limit` caps the rows of every SELECT without a LIMIT. As an
    `init_connect` statement (or a GLOBAL default, below) it reaches parity's
    session. With a limit of 2, `columns()` sees only `id` and `a`, so the
    planted change in column `c` is never looked at: identical, exit 0, and
    the sample check (which uses the same column list) agrees. Shared MySQL,
    session-level only."""
    conn = mysql_connect(mysql_url)
    a, b = f"rtg_limit_a_{RUN}", f"rtg_limit_b_{RUN}"
    try:
        _mysql_wide_tables(conn, a, b)
        _init_connect_hook(monkeypatch, "set session sql_select_limit = 2")
        result = run_diff(open_mysql(mysql_url, "A"), open_mysql(mysql_url, "B"), a, b)
        assert found(result) == [(150, "different", ("c",))]
    finally:
        mysql_run(conn, f"drop table if exists `{a}`", f"drop table if exists `{b}`")
        conn.close()


@pytest.fixture
def mysql_root():
    """Root on the Docker MySQL (GLOBAL settings live there), or skip."""
    conn = mysql_connect(MYSQL_ROOT_URL)
    yield conn
    conn.close()


@pytest.mark.parametrize(
    ("setting", "reset"),
    [
        ("set global sql_select_limit = 2", "set global sql_select_limit = default"),
        (
            "set global init_connect = 'SET sql_select_limit = 2'",
            "set global init_connect = ''",
        ),
    ],
    ids=["global", "init_connect"],
)
def test_rt_g1_mysql_sql_select_limit_server_default(mysql_root, setting, reset):
    """The same as a real server setting on the Docker MySQL 8.0, read by a
    non-root login (root skips `init_connect`)."""
    a, b = f"rtg_glimit_a_{RUN}", f"rtg_glimit_b_{RUN}"
    _mysql_wide_tables(mysql_root, a, b)
    mysql_run(mysql_root, setting)
    try:
        result = run_diff(
            get_dialect(MYSQL_USER_URL, side="A"),
            get_dialect(MYSQL_USER_URL, side="B"),
            a,
            b,
        )
    finally:
        mysql_run(
            mysql_root,
            reset,
            f"drop table if exists `{a}`",
            f"drop table if exists `{b}`",
        )
    assert found(result) == [(150, "different", ("c",))]


@pytest.mark.parametrize(
    "statement",
    [
        "SET NAMES latin1",
        "SET character_set_results = latin1, collation_connection = latin1_swedish_ci",
        "SET NAMES utf8mb3",
        "SET time_zone = '+09:00', div_precision_increment = 0",
        "SET sql_mode = 'ANSI,NO_BACKSLASH_ESCAPES,PAD_CHAR_TO_FULL_LENGTH'",
    ],
)
def test_mysql_real_init_connect_does_not_move_the_verdict(mysql_root, statement):
    """A real server-side `init_connect` (not a client-side model of it): the
    connector's own `SET NAMES utf8mb4` and the dialect's session pins run
    after it, so the emoji pair, the backslash and the CHAR value compare as
    they should and only the planted rows differ."""
    a, b = f"rtg_ic_a_{RUN}", f"rtg_ic_b_{RUN}"
    for t, emoji, ch in ((a, "x\U0001f600", "ab"), (b, "x\U0001f601", "ab")):
        mysql_run(
            mysql_root,
            f"drop table if exists `{t}`",
            f"create table `{t}` (id int primary key, v varchar(20), ch char(5), "
            "ts timestamp(6) null) charset utf8mb4",
        )
        cur = mysql_root.cursor()
        cur.execute("set time_zone = '+00:00'")
        cur.executemany(
            f"insert into `{t}` values (%s, %s, %s, %s)",
            [(1, emoji, ch, "2024-06-15 12:00:00"), (2, "a\\b", "x", None)],
        )
        cur.close()
    mysql_run(mysql_root, f"set global init_connect = {_sql_str(statement)}")
    try:
        result = run_diff(
            get_dialect(MYSQL_USER_URL, side="A"),
            get_dialect(MYSQL_USER_URL, side="B"),
            a,
            b,
        )
    finally:
        mysql_run(
            mysql_root,
            "set global init_connect = ''",
            f"drop table if exists `{a}`",
            f"drop table if exists `{b}`",
        )
    assert found(result) == [(1, "different", ("v",))]
    assert result.diffs[0].values_a == {"v": "x\U0001f600"}


def _sql_str(value: str) -> str:
    """A MySQL single-quoted string literal (quotes doubled, no backslashes used)."""
    return "'" + value.replace("'", "''") + "'"


def test_rt_g3_mysql_escaped_text_past_max_allowed_packet_is_not_null(mysql_root):
    """A 20,000-character TEXT holding one backslash, against NULL, on a server
    whose max_allowed_packet (GLOBAL) is 16 KiB - stored while the limit was
    higher, as data outlives configuration. Doubling the backslash makes
    REPLACE grow the value, and REPLACE returns NULL for any result past the
    limit. `_binary_text` guards exactly this for binary columns; text has no
    guard, so the coalesce turned the value into the NULL sentinel. Fixed as
    binary is: the value is refused, naming the side and its size - a loud
    stop, never a NULL."""
    a, b = f"rtg_pkt_a_{RUN}", f"rtg_pkt_b_{RUN}"
    for t in (a, b):
        mysql_run(
            mysql_root,
            f"drop table if exists `{t}`",
            f"create table `{t}` (id int primary key, v longtext) charset utf8mb4",
        )
    mysql_run(
        mysql_root,
        f"insert into `{a}` values (1, concat(char(92), repeat('x', 20000))), (2, 'ok')",
        f"insert into `{b}` values (1, null), (2, 'ok')",
        "set global max_allowed_packet = 16384",
    )
    try:
        with pytest.raises(ValueError, match=r"side A.*max_allowed_packet"):
            run_diff(
                get_dialect(MYSQL_USER_URL, side="A"),
                get_dialect(MYSQL_USER_URL, side="B"),
                a,
                b,
            )
    finally:
        mysql_run(
            mysql_root,
            "set global max_allowed_packet = 67108864",
            f"drop table if exists `{a}`",
            f"drop table if exists `{b}`",
        )


def test_mysql_long_text_without_escapes_past_max_allowed_packet_is_found(mysql_root):
    """The control for RT-G3: without a backslash or chr(31) REPLACE does not
    grow the value and never hits the limit, so the same 20,000-character text
    against NULL is found."""
    a, b = f"rtg_pkt2_a_{RUN}", f"rtg_pkt2_b_{RUN}"
    for t in (a, b):
        mysql_run(
            mysql_root,
            f"drop table if exists `{t}`",
            f"create table `{t}` (id int primary key, v longtext) charset utf8mb4",
        )
    mysql_run(
        mysql_root,
        f"insert into `{a}` values (1, repeat('x', 20000)), (2, 'ok')",
        f"insert into `{b}` values (1, null), (2, 'ok')",
        "set global max_allowed_packet = 16384",
    )
    try:
        result = run_diff(
            get_dialect(MYSQL_USER_URL, side="A"),
            get_dialect(MYSQL_USER_URL, side="B"),
            a,
            b,
        )
    finally:
        mysql_run(
            mysql_root,
            "set global max_allowed_packet = 67108864",
            f"drop table if exists `{a}`",
            f"drop table if exists `{b}`",
        )
    assert found(result) == [(1, "different", ("v",))]


@pytest.mark.parametrize("name", ["rtg_Lat", "rtg_lat", "RTG_LAT"])
def test_mysql_latin1_server_without_handshake_and_folded_names(tmp_path, name):
    """A legacy server started with --character-set-server=latin1
    --skip-character-set-client-handshake --lower-case-table-names=1 (Docker,
    `rt-ver-my80hs`): a table created without a charset is latin1; its 'café'
    hashes as the UTF-8 text DuckDB holds, the mojibake 'Ã©' does not equal
    'é', and the table is found under any spelling of its name."""
    root = mysql_connect(MYSQL_LATIN1_ROOT_URL)
    try:
        mysql_run(
            root,
            "drop table if exists rtg_Lat",
            "create table rtg_Lat (id int primary key, v varchar(20), c char(5))",
        )
        cur = root.cursor()
        cur.executemany(
            "insert into rtg_Lat values (%s, %s, %s)",
            [(1, "café", "é"), (2, "x", "ab"), (3, "Ã©", "q")],
        )
        cur.close()
        duck = duck_file(
            tmp_path,
            "lat",
            "create table t (id int, v varchar, c varchar)",
            "insert into t values (1, 'café', 'é'), (2, 'X', 'ab'), (3, 'é', 'q')",
        )
        user = MYSQL_LATIN1_ROOT_URL.replace("root:rtg@", "rtg:rtg@")
        result = run_diff(
            get_dialect(user, side="A"), open_duckdb(duck, "B"), name, "main.t"
        )
    finally:
        mysql_run(root, "drop table if exists rtg_Lat")
        root.close()
    assert found(result) == [(2, "different", ("v",)), (3, "different", ("v",))]


def test_mysql_max_join_size_fails_loudly_naming_the_side(mysql_url, monkeypatch):
    """`max_join_size` (a DBA guard against runaway queries) makes the
    full-table checksum refuse to run. That cannot be a false match; it must
    say which side and table, and it does."""
    conn = mysql_connect(mysql_url)
    a, b = f"rtg_mjs_a_{RUN}", f"rtg_mjs_b_{RUN}"
    try:
        _mysql_wide_tables(conn, a, b)
        _init_connect_hook(monkeypatch, "set session max_join_size = 10")
        with pytest.raises(ValueError, match=r"side A: mysql.*" + a):
            run_diff(open_mysql(mysql_url, "A"), open_mysql(mysql_url, "B"), a, b)
    finally:
        mysql_run(conn, f"drop table if exists `{a}`", f"drop table if exists `{b}`")
        conn.close()


# ============================================================== PostgreSQL

#: A table holding one value of every type family PostgreSQL renders, for the
#: GUC sweep: escapes, infinities, BC dates, every unmapped type's text form.
PG_RICH_DDL = (
    "id int primary key, i bigint, n numeric(20,6), f float8, r real, b bool, "
    "t text, vc varchar(10), ch char(5), d date, ts timestamp, tz timestamptz, "
    "tm time, ttz timetz, iv interval, by bytea, u uuid, j json, jb jsonb, x xml, "
    "m money, ai int[], at text[], ip inet, tv tsvector, bt bit(4), pt point, "
    'ci text collate "C"'
)
PG_RICH_ROWS = [
    (
        "(1, 9223372036854775807, 12345678901234.123456, 0.1, 1.5, true, 'héllo 😀', 'ab', "
        "'ab', '2024-02-29', '2024-02-29 13:04:05.123456', '2024-06-15 12:00:00+00', "
        "'13:04:05.5', '13:04:05+02', '1 year 2 mons 3 days 04:05:06.7', '\\x00ff5c', "
        "'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', '{\"b\": 1, \"a\": [1, 2.50]}', "
        "'{\"b\": 1, \"a\": [1, 2.50]}', '<a>x</a>', 1234.56, '{1,NULL,3}', "
        "'{\"a b\",NULL,\"\"}', '192.168.0.1/24', 'a fat cat', B'1010', '(1.5,2)', 'Zz')"
    ),
    "(2" + ", null" * 27 + ")",
    (
        "(3, -1, -0.0000005, 1e15+0.5, -4088288.25, false, E'a\\\\N\\x1fb', '', '', "
        "'infinity', '-infinity', 'infinity', '00:00', '00:00+00', '-1 mon 2 days -00:00:01', "
        "'\\x', '00000000-0000-0000-0000-000000000000', '[]', '{}', '', -0.5, '{}', '{}', "
        "'::1', '', B'0000', '(0,-0)', 'a')"
    ),
    (
        "(4, 0, 0, 'NaN', '-Infinity', true, 'x', 'x', 'x', '0044-03-15 BC', "
        "'0044-03-15 12:00 BC', '1999-12-31 23:59:59.999999-12', '24:00', '23:59:59-1459', "
        "'P1Y2M3DT4H5M6S', '\\xdeadbeef', 'ffffffff-ffff-ffff-ffff-ffffffffffff', "
        "'\"\\u00e9\"', '\"\\u00e9\"', '<!-- c --><a/>', 0, '{{1,2},{3,4}}', '{\"\\\\\"}', "
        "'10.0.0.0/8', 'b:1 a:2', B'1111', '(1e300,1e-300)', 'A')"
    ),
]


@pytest.fixture(scope="module")
def pg_rich(pg_url):
    """`parity_rtg.rich_<run>` on the shared PostgreSQL, dropped afterwards."""
    conn = pg_connect(pg_url)
    table = f"parity_rtg.rich_{RUN}"
    conn.execute("create schema if not exists parity_rtg")
    conn.execute(f"create table {table} ({PG_RICH_DDL})")
    conn.execute(f"insert into {table} values " + ", ".join(PG_RICH_ROWS))
    yield table
    conn.execute(f"drop table if exists {table}")
    conn.close()


def _pg_fingerprint(url: str, table: str):
    """What a session renders for `table`: logical types, the canonical text of
    every value, the row-hash sum, and the sample check's raw values."""
    d = get_dialect(url, side="A")
    try:
        cols = d.columns(table)
        q = d.qualify(table)
        sel = ", ".join(d.normalize(c) for c in cols)
        out = {
            "types": [(c.name, c.logical_type.name) for c in cols],
            "text": d.query(f"select {sel} from {q} order by 1"),
            "hash": str(d.query(f"select {d.sum_wide(d.row_hash(cols))} from {q}")[0][0]),
        }
        # Column by column: psycopg cannot load every value PostgreSQL holds
        # (time '24:00', see RT-G12), and that must not hide the others.
        sample = []
        for c in cols:
            try:
                sql = f'select {d.sample_select(c)} from {q} order by "id"'
                sample.append([repr(r[0]) for r in d.query(sql)])
            except Exception as exc:  # the error text is the fingerprint
                d.close()
                d = get_dialect(url, side="A")
                sample.append(f"{type(exc).__name__}: {exc}")
        out["sample"] = sample
        return out
    finally:
        d.close()


#: Parameters set elsewhere in this file (RT-G7) or that libpq cannot set at
#: backend start (transaction characteristics: "must be called before any query").
_SWEEP_EXCLUDED = {
    "quote_all_identifiers",
    "transaction_isolation",
    "transaction_read_only",
    "transaction_deferrable",
}
#: Free-form parameters worth a non-default value besides every bool and enum.
_SWEEP_EXTRA = {
    "DateStyle": ["SQL, DMY", "German", "Postgres, YMD"],
    "TimeZone": ["Asia/Kathmandu", "America/St_Johns"],
    "extra_float_digits": ["0", "-15", "3"],
    "search_path": ["pg_temp", "public, pg_catalog"],
    "lc_monetary": ["C.UTF-8"],
    "lc_numeric": ["C.UTF-8"],
    "lc_time": ["C.UTF-8"],
    "default_text_search_config": ["pg_catalog.german"],
    "timezone_abbreviations": ["India", "Australia"],
    "work_mem": ["64kB"],
    "jit_above_cost": ["0"],
    "parallel_setup_cost": ["0"],
    "parallel_tuple_cost": ["0"],
    "min_parallel_table_scan_size": ["0"],
    "max_parallel_workers_per_gather": ["8"],
}


def test_pg_no_user_settable_parameter_moves_the_canonical_text(pg_url, pg_rich):
    """Every parameter a role or database default can carry (context 'user'),
    at every value of every bool and enum, plus chosen free-form values: the
    session it starts renders exactly the logical types, canonical text, row
    hashes and sample values of a default session - or cannot connect at all,
    which is loud. The single exception is RT-G7 (`quote_all_identifiers`),
    pinned separately. Run against the shared server, session-level only."""
    conn = pg_connect(pg_url)
    gucs = conn.execute(
        "select name, vartype, enumvals, setting from pg_settings "
        "where context = 'user' order by name"
    ).fetchall()
    conn.close()
    trials: list[tuple[str, str]] = []
    for name, vartype, enumvals, setting in gucs:
        if name in _SWEEP_EXCLUDED:
            continue
        if vartype == "bool":
            trials.append((name, "off" if setting == "on" else "on"))
        elif vartype == "enum":
            trials += [(name, v) for v in enumvals if v != setting]
    known = {g[0].lower() for g in gucs}
    trials += [(k, v) for k, vs in _SWEEP_EXTRA.items() for v in vs if k.lower() in known]
    base = _pg_fingerprint(pg_url, pg_rich)
    moved, refused = [], []
    for name, value in trials:
        try:
            got = _pg_fingerprint(pg_options(pg_url, **{name: value}), pg_rich)
        except ValueError as exc:
            if "could not connect" not in str(exc):
                moved.append(f"{name}={value}: {exc}")
            else:
                refused.append(name)
            continue
        changed = [k for k in base if got[k] != base[k]]
        if changed:
            moved.append(f"{name}={value}: {changed}")
    assert len(trials) > 100, len(trials)
    assert not moved, "\n".join(moved)


def test_rt_g7_pg_quote_all_identifiers_keeps_the_logical_types(pg_url, pg_rich):
    """A role or database default `quote_all_identifiers = on` (pg_dump's
    `--quote-all-identifiers`, kept for a migration role) must not change how a
    column is classified: `format_type` quotes every name it does not hard-code
    (`date`, `text`, `uuid`, `bytea`, `jsonb` ...) under it."""
    plain = get_dialect(pg_url, side="A")
    quoted = get_dialect(pg_options(pg_url, quote_all_identifiers="on"), side="B")
    try:
        want = [(c.name, c.logical_type) for c in plain.columns(pg_rich)]
        got = [(c.name, c.logical_type) for c in quoted.columns(pg_rich)]
    finally:
        plain.close()
        quoted.close()
    assert got == want


def test_rt_g7_pg_quote_all_identifiers_same_table_reads_identical(pg_url, pg_rich):
    """One table, read by a default session and by one with
    quote_all_identifiers on: identical data must read identical."""
    a = get_dialect(pg_url, side="A")
    b = get_dialect(pg_options(pg_url, quote_all_identifiers="on"), side="B")
    result = run_diff(a, b, pg_rich, pg_rich)
    assert found(result) == []
    assert not any("compared as text" in w for w in result.warnings), result.warnings


def test_pg_role_statement_timeout_fails_loudly_naming_the_side(pg_url):
    """A role default statement_timeout shorter than the full-table hash pass
    kills the diff. Loud, and it names the side and the table. (300,000 rows,
    so the hash pass reliably outlasts 1 ms.)"""
    conn = pg_connect(pg_url)
    conn.execute("create schema if not exists parity_rtg")
    t = f"parity_rtg.slow_{RUN}"
    try:
        conn.execute(f"create table {t} as select i as id, md5(i::text) as v "
                     "from generate_series(1, 300000) i")  # fmt: skip
        a = get_dialect(pg_options(pg_url, statement_timeout="1ms"), side="A")
        b = get_dialect(pg_url, side="B")
        with pytest.raises(ValueError, match=rf"side A: postgres.*slow_{RUN}"):
            run_diff(a, b, t, t)
    finally:
        conn.execute(f"drop table if exists {t}")
        conn.close()


def test_rt_g6_pg_row_level_security_is_not_silent(pg_url):
    """Legacy tables often carry RLS (tenant isolation). Here the policy hides
    ids above 50 from the reading role, and the only difference is row 51: the
    run says identical over 50 rows of a 100-row table. A tool that cannot see
    every row must say so (CLAUDE.md section 8: never claim exact what is not)."""
    conn = pg_connect(pg_url)
    conn.execute("create schema if not exists parity_rtg")
    a, b = f"parity_rtg.rls_a_{RUN}", f"parity_rtg.rls_b_{RUN}"
    try:
        for t in (a, b):
            conn.execute(f"create table {t} (id int primary key, v text)")
            conn.execute(
                f"insert into {t} select i, 'v' || i from generate_series(1, 100) i"
            )
            conn.execute(f"alter table {t} enable row level security")
            # FORCE: the owner (the test role) is subject to it too, as any
            # non-owner reader is without it.
            conn.execute(f"alter table {t} force row level security")
            conn.execute(f"create policy rtg_p on {t} using (id <= 50)")
        conn.execute(f"update {b} set v = 'PLANT' where id = 51")
        result = run_diff(open_pg(pg_url, "A"), open_pg(pg_url, "B"), a, b)
    finally:
        conn.execute(f"drop table if exists {a}")
        conn.execute(f"drop table if exists {b}")
        conn.close()
    warned = any("row" in w.lower() and "security" in w.lower() for w in result.warnings)
    assert warned or not result.identical, (found(result), result.warnings)


def test_rt_g12_pg_time_2400_does_not_stop_the_sample_check(pg_url):
    """`time` is UNKNOWN to the rules, so the check could never compare it, but
    its raw value is still fetched, and one '24:00' in any row ends the check
    for every column with 'could not run'."""
    conn = pg_connect(pg_url)
    conn.execute("create schema if not exists parity_rtg")
    a, b = f"parity_rtg.t24_a_{RUN}", f"parity_rtg.t24_b_{RUN}"
    try:
        conn.execute(f"create table {a} (id int primary key, tm time, v text)")
        conn.execute(f"insert into {a} values (1, '24:00', 'a'), (2, '12:00', 'b')")
        conn.execute(f"create table {b} as select * from {a}")
        conn.execute(f"update {b} set v = 'B' where id = 2")
        result = run_diff(open_pg(pg_url, "A"), open_pg(pg_url, "B"), a, b)
    finally:
        conn.execute(f"drop table if exists {a}")
        conn.execute(f"drop table if exists {b}")
        conn.close()
    assert found(result) == [(2, "different", ("v",))]
    assert result.verification is not None and result.verification.error is None, (
        result.verification
    )


@pytest.mark.sample_check_may_disagree  # the sample check sees the mojibake row
@pytest.mark.parametrize("encoding", ["LATIN1", "WIN1252"])
def test_rt_g4_pg_non_utf8_database_hashes_text_as_utf8(tmp_path, encoding):
    """A legacy PostgreSQL database in LATIN1 or WIN1252 against DuckDB. Row 1
    holds 'é' on both sides and must match; row 2 holds the classic mojibake
    'Ã©' (UTF-8 é read as Latin-1) against a correct 'é' and must differ. The
    walk gets both wrong: row 1 is an unexplained checksum mismatch, row 2 a
    match. Docker PostgreSQL (CREATE DATABASE needs the right)."""
    admin = pg_connect(PG_ADMIN_URL)
    db = f"rtg_enc_{encoding.lower()}"
    admin.execute(f"drop database if exists {db}")
    admin.execute(
        f"create database {db} encoding '{encoding}' lc_collate 'C' lc_ctype 'C' "
        "template template0"
    )
    url = pg_with_db(PG_ADMIN_URL, db)
    try:
        c = pg_connect(url)
        c.execute("create table t (id int primary key, s text)")
        c.execute("insert into t values (1, 'é'), (2, 'Ã©'), (3, 'plain')")
        c.close()
        duck = duck_file(
            tmp_path,
            "enc",
            "create table t (id int, s varchar)",
            "insert into t values (1, 'é'), (2, 'é'), (3, 'plain')",
        )
        result = run_diff(open_pg(url, "A"), open_duckdb(duck, "B"), "public.t", "main.t")
    finally:
        admin.execute(f"drop database if exists {db} with (force)")
        admin.close()
    assert not result.unexplained, result.unexplained
    assert found(result) == [(2, "different", ("s",))]


# ============================================================== SQL Server

_MS_DDL = (
    "id int primary key, vc varchar(40), nv nvarchar(40), ch char(6), nch nchar(6), "
    "d date, dt datetime, dt2 datetime2(6), x decimal(18,8), u uniqueidentifier, b bit"
)
_MS_ROWS = [
    (1, "caf\u00e9", "caf\u00e9 \U0001f600", "ab", "ab", "2024-02-29",
     "2024-10-27 02:30:00.003", "2024-03-31 02:30:00.123456", "1.12345678",
     "A0EEBC99-9C0B-4EF8-BB6D-6BB9BD380A11", 1),
    (2, None, None, None, None, None, None, None, None, None, None),
    (3, "a\\Nb", "\u65e5\u672c", "x ", "\u00e9", "0001-01-01", "1753-01-01 00:00:00",
     "9999-12-31 23:59:59.999999", "-0.00000050", "00000000-0000-0000-0000-000000000000", 0),
    (4, "plant", "Z\u0308", "ABC", "ABC", "2024-01-01", "2024-01-01 00:00:00.997",
     "2024-01-01 00:00:00", "2", "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF", 1),
]  # fmt: skip
#: The DuckDB twin; row 4's `vc` is the planted difference.
_DUCK_ROWS = (
    "(1, 'café', 'café 😀', 'ab', 'ab', '2024-02-29', '2024-10-27 02:30:00.003', "
    "'2024-03-31 02:30:00.123456', 1.12345678, 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', true), "
    "(2, null, null, null, null, null, null, null, null, null, null), "
    "(3, 'a\\Nb', '日本', 'x', 'é', '0001-01-01', '1753-01-01 00:00:00', "
    "'9999-12-31 23:59:59.999999', -0.0000005, '00000000-0000-0000-0000-000000000000', false), "
    "(4, 'PLANT', 'Z\u0308', 'ABC', 'ABC', '2024-01-01', '2024-01-01 00:00:00.997', "
    "'2024-01-01 00:00:00', 2, 'ffffffff-ffff-ffff-ffff-ffffffffffff', true)"
)


def _ms_database(name: str, collation: str, compat: int, *options: str) -> str:
    """Create database `name` on the Docker SQL Server; return its parity URL."""
    mssql_drop_database(MSSQL_ADMIN_URL, name)
    mssql_exec(
        MSSQL_ADMIN_URL,
        "master",
        f"create database [{name}] collate {collation}",
        f"alter database [{name}] set compatibility_level = {compat}",
        f"alter database [{name}] set allow_snapshot_isolation on",
        *(f"alter database [{name}] set {o}" for o in options),
    )
    return mssql_url(MSSQL_ADMIN_URL, name)


def _ms_types_table(url: str, ddl: str = _MS_DDL, rows=_MS_ROWS) -> list[tuple]:
    """Create and fill `dbo.t` through bound parameters; return it as read back."""
    import mssql_python

    from parity.dialects.mssql_dialect import parse_mssql_url

    odbc, _ = parse_mssql_url(url)
    conn = mssql_python.connect(odbc, autocommit=True)
    try:
        cur = conn.cursor()
        cur.execute(f"create table dbo.t ({ddl})")
        marks = ", ".join("?" * len(rows[0]))
        for r in rows:
            cur.execute(f"insert into dbo.t values ({marks})", list(r))
        cur.execute("select * from dbo.t order by id")
        back = [tuple(r) for r in cur.fetchall()]
        cur.close()
        return back
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("collation", "compat"),
    [
        ("SQL_Latin1_General_CP1_CI_AS", 130),
        ("Latin1_General_100_CS_AS_SC_UTF8", 160),
        ("Japanese_XJIS_140_CS_AS_KS_WS", 160),
        ("Turkish_100_CS_AS", 140),
        ("Latin1_General_BIN", 150),
        ("Cyrillic_General_CI_AS", 100),
        ("SQL_Latin1_General_CP1_CS_AS", 110),
    ],
)
def test_mssql_database_collation_and_compatibility_level(tmp_path, collation, compat):
    """Database collations (SQL_*, code-page, binary, Turkish casing, DBCS,
    _SC_UTF8) at compatibility levels 100-160, on a table without FLOAT
    (RT-G5 covers that). A varchar holding 'café' in a code page without é is
    stored as the server stores it (best fit); the verdict must be exactly
    what the stored values say, plus the planted row 4."""
    db = f"rtg_coll_{compat}_{RUN}"
    url = _ms_database(db, collation, compat)
    try:
        back = _ms_types_table(url)
        duck = duck_file(
            tmp_path,
            "ms",
            "create table t (id int, vc varchar, nv varchar, ch varchar, nch varchar, d date, "
            "dt timestamp, dt2 timestamp, x decimal(18,8), u uuid, b boolean)",
            f"insert into t values {_DUCK_ROWS}",
        )
        result = run_diff(open_mssql(url, "A"), open_duckdb(duck, "B"), "dbo.t", "main.t")
    finally:
        mssql_drop_database(MSSQL_ADMIN_URL, db)
    lossy = [r[0] for r in back if r[0] == 1 and r[1] != "caf\u00e9"]
    want = [(k, "different", ("vc",)) for k in lossy] + [(4, "different", ("vc",))]
    assert found(result) == want


def test_mssql_contained_user_in_rcsi_database_without_snapshot(tmp_path):
    """A contained-database user (no server login) in a database with
    READ_COMMITTED_SNAPSHOT on and ALLOW_SNAPSHOT_ISOLATION off: the dialect
    connects, finds the planted row, and says the walk is not one snapshot."""
    db = f"rtg_contained_{RUN}"
    mssql_exec(
        MSSQL_ADMIN_URL,
        "master",
        "exec sp_configure 'contained database authentication', 1; reconfigure;",
    )
    mssql_drop_database(MSSQL_ADMIN_URL, db)
    mssql_exec(
        MSSQL_ADMIN_URL,
        "master",
        f"create database [{db}] containment = partial collate Latin1_General_100_CI_AS_SC_UTF8",
        f"alter database [{db}] set read_committed_snapshot on",
    )
    try:
        mssql_exec(
            MSSQL_ADMIN_URL,
            db,
            "create user rtg_c with password = 'Rtg!Contained2026'",
            "alter role db_datareader add member rtg_c",
            "create table dbo.t (id int primary key, v nvarchar(10))",
            "insert into dbo.t values (1, N'a'), (2, N'b')",
            "create table dbo.t2 (id int primary key, v nvarchar(10))",
            "insert into dbo.t2 values (1, N'a'), (2, N'B')",
        )
        u = mssql_url(MSSQL_ADMIN_URL, db).replace(
            "sa:Rtg!2026xyzQ@", "rtg_c:Rtg!Contained2026@"
        )
        result = run_diff(open_mssql(u, "A"), open_mssql(u, "B"), "dbo.t", "dbo.t2")
    finally:
        mssql_drop_database(MSSQL_ADMIN_URL, db)
        # A server with contained databases refuses a non-zero 'user options'
        # (the RT-G8 tests), so the switch goes back once ours is gone.
        mssql_exec(
            MSSQL_ADMIN_URL,
            "master",
            "exec sp_configure 'contained database authentication', 0; reconfigure;",
        )
    assert found(result) == [(2, "different", ("v",))]
    assert any("READ COMMITTED" in w for w in result.warnings)


def _ms_user_options(value: int) -> None:
    """Set the server-wide `user options` default on the Docker SQL Server."""
    mssql_exec(
        MSSQL_ADMIN_URL,
        "master",
        f"exec sp_configure 'user options', {value}; reconfigure;",
    )


def _ms_diff_with_user_options(tmp_path, value: int):
    """Diff the mixed-type table under server `user options` = `value`."""
    db = f"rtg_uo_{value}_{RUN}"
    url = _ms_database(db, "Latin1_General_100_CI_AS_SC_UTF8", 160)
    try:
        _ms_user_options(value)
        _ms_types_table(url)
        duck = duck_file(
            tmp_path,
            "uo",
            "create table t (id int, vc varchar, nv varchar, ch varchar, nch varchar, d date, "
            "dt timestamp, dt2 timestamp, x decimal(18,8), u uuid, b boolean)",
            f"insert into t values {_DUCK_ROWS}",
        )
        return run_diff(open_mssql(url, "A"), open_duckdb(duck, "B"), "dbo.t", "main.t")
    finally:
        _ms_user_options(0)
        mssql_drop_database(MSSQL_ADMIN_URL, db)


def test_mssql_server_user_options_the_driver_resets(tmp_path):
    """sp_configure 'user options' = IMPLICIT_TRANSACTIONS | ARITHIGNORE |
    NOCOUNT | XACT_ABORT (17026), a server-wide session default: the ODBC
    driver and the dialect neutralise all four, only row 4 differs."""
    result = _ms_diff_with_user_options(tmp_path, 2 | 128 | 512 | 16384)
    assert found(result) == [(4, "different", ("vc",))]


def test_rt_g8_mssql_server_user_options_numeric_roundabort(tmp_path):
    """The same table under 'user options' = 8192: every session on the server
    starts with NUMERIC_ROUNDABORT ON, which the driver does not reset."""
    result = _ms_diff_with_user_options(tmp_path, 8192)
    assert found(result) == [(4, "different", ("vc",))]


@pytest.mark.parametrize("compat", [100, 110, 120])
def test_rt_g5_mssql_float_below_compatibility_level_130(tmp_path, compat):
    """RT-G5: below compatibility level 130 SQL Server converts float to
    decimal through 17 significant digits, so the canonical FLOAT form (built
    on the exact conversion) rendered -262.4724295 as -262.472429 - a false
    match against DuckDB. FLOAT columns there are now refused, naming the
    side, the column and the level; every other type renders as at 130+."""
    db = f"rtg_compat_{compat}_{RUN}"
    url = _ms_database(db, "Latin1_General_100_CI_AS_SC_UTF8", compat)
    try:
        _ms_types_table(
            url, "id int primary key, f float", [(1, -262.4724295), (2, 2.0**63)]
        )
        duck = duck_file(
            tmp_path,
            "compat",
            "create table t (id int, f double)",
            "insert into t values (1, -262.472429), (2, 9223372036854775808.0)",
        )
        with pytest.raises(
            ValueError, match=rf"side A.*'f'.*compatibility level {compat}"
        ):
            run_diff(open_mssql(url, "A"), open_duckdb(duck, "B"), "dbo.t", "main.t")
    finally:
        mssql_drop_database(MSSQL_ADMIN_URL, db)


def test_mssql_float_at_compatibility_level_130(tmp_path):
    """The control for RT-G5: at 130 the same rows give exactly the planted row."""
    db = f"rtg_compat_130_{RUN}"
    url = _ms_database(db, "Latin1_General_100_CI_AS_SC_UTF8", 130)
    try:
        _ms_types_table(
            url, "id int primary key, f float", [(1, -262.4724295), (2, 2.0**63)]
        )
        duck = duck_file(
            tmp_path,
            "compat130",
            "create table t (id int, f double)",
            "insert into t values (1, -262.472429), (2, 9223372036854775808.0)",
        )
        result = run_diff(open_mssql(url, "A"), open_duckdb(duck, "B"), "dbo.t", "main.t")
    finally:
        mssql_drop_database(MSSQL_ADMIN_URL, db)
    assert found(result) == [(1, "different", ("f",))]


# =============================================================== Snowflake


def _sf_exec(url: str, *statements: str) -> list:
    """Run statements on Snowflake through a parity dialect's connection."""
    d = get_dialect(url, side="W")
    cur = d._conn.cursor()
    out = []
    try:
        for sql in statements:
            cur.execute(sql)
            out.append(cur.fetchall())
    finally:
        cur.close()
        d.close()
    return out


def _sf_with_user_parameter(snowflake_url: str, parameter: str):
    """Diff RTG_A/RTG_B (differing only in column "c" of row 150) with a
    user-level parameter set for the login, then unset it."""
    me = _sf_exec(snowflake_url, "select current_user()")[0][0][0]
    rows = ", ".join(f"({i}, 'a{i}', 'b{i}', 'c{i}')" for i in range(1, 201))
    a, b = f"RTG_A_{RUN.upper()}", f"RTG_B_{RUN.upper()}"
    _sf_exec(
        snowflake_url,
        f'create or replace table {a} ("id" int, "a" varchar, "b" varchar, "c" varchar)',
        f"insert into {a} values {rows}",
        f"create or replace table {b} clone {a}",
        f"""update {b} set "c" = 'PLANT' where "id" = 150""",
        f'alter user "{me}" set {parameter}',
    )
    try:
        return run_diff(
            get_dialect(snowflake_url, side="A"),
            get_dialect(snowflake_url, side="B"),
            a,
            b,
        )
    finally:
        _sf_exec(
            snowflake_url,
            f'alter user "{me}" unset {parameter.split()[0]}',
            f"drop table if exists {a}",
            f"drop table if exists {b}",
        )


def test_rt_g2_snowflake_rows_per_resultset(snowflake_url):
    """ROWS_PER_RESULTSET = 2 on the login, as an administrator might set it
    to protect a shared warehouse: parity sees columns "id" and "a" only and
    calls two tables identical that differ in column "c". Live, two tiny tables."""
    result = _sf_with_user_parameter(snowflake_url, "ROWS_PER_RESULTSET = 2")
    assert found(result) == [(150, "different", ("c",))]


def test_snowflake_quoted_identifiers_ignore_case(snowflake_url):
    """QUOTED_IDENTIFIERS_IGNORE_CASE = TRUE on the login, against a table
    whose lower-case columns were created quoted: the dialect's quoted names
    still resolve, and the planted row is found."""
    result = _sf_with_user_parameter(
        snowflake_url, "QUOTED_IDENTIFIERS_IGNORE_CASE = TRUE"
    )
    assert found(result) == [(150, "different", ("c",))]


#: Output-format parameters a DBA sets per user or account. The dialect
#: renders dates and timestamps through TO_CHAR with explicit formats, so none
#: of them may reach the canonical text.
_SF_FORMATS = (
    "DATE_OUTPUT_FORMAT = 'DD.MM.YYYY' "
    "TIMESTAMP_NTZ_OUTPUT_FORMAT = 'DD.MM.YYYY HH24:MI' "
    "TIMESTAMP_TZ_OUTPUT_FORMAT = 'DY, DD MON YYYY HH24:MI:SS TZHTZM' "
    "TIMESTAMP_LTZ_OUTPUT_FORMAT = 'YYYY-MM-DD' "
    "TIME_OUTPUT_FORMAT = 'HH24:MI' "
    "BINARY_OUTPUT_FORMAT = 'BASE64' "
    "TIMEZONE = 'Asia/Kathmandu' "
    "WEEK_START = 1"
)


def test_snowflake_user_output_formats_do_not_move_the_verdict(snowflake_url, tmp_path):
    """A login carrying every output-format parameter at a non-default value
    (and a zone, which the dialect re-pins to UTC): Snowflake against DuckDB,
    with dates, naive and zoned timestamps, decimals, doubles and text, finds
    exactly the planted row. Live, one tiny table."""
    me = _sf_exec(snowflake_url, "select current_user()")[0][0][0]
    t = f"RTG_FMT_{RUN.upper()}"
    _sf_exec(
        snowflake_url,
        f'create or replace table {t} ("id" int, "d" date, "ts" timestamp_ntz(6), '
        '"tz" timestamp_tz(6), "x" number(12,3), "f" float, "s" varchar)',
        f"insert into {t} select 1, '2024-02-29', '2024-02-29 13:04:05.123456', "
        "'2024-06-15 12:00:00.5 +02:00', 1.5, 0.1, 'a' union all "
        "select 2, null, null, null, null, null, null union all "
        "select 3, '0001-01-01', '1970-01-01 00:00:00', '1999-12-31 23:59:59 -12:00', "
        "-2.25, 1e15, 'plant'",
        f'alter user "{me}" set {_SF_FORMATS}',
    )
    duck = duck_file(
        tmp_path,
        "sf_fmt",
        "create table t (id int, d date, ts timestamp, tz timestamptz, x decimal(12,3), "
        "f double, s varchar)",
        "insert into t values (1, '2024-02-29', '2024-02-29 13:04:05.123456', "
        "'2024-06-15 10:00:00.5+00', 1.5, 0.1, 'a'), "
        "(2, null, null, null, null, null, null), "
        "(3, '0001-01-01', '1970-01-01 00:00:00', '2000-01-01 11:59:59+00', -2.25, 1e15, 'PLANT')",
    )
    names = [p.split()[-1] for p in _SF_FORMATS.split(" = ")[:-1]]
    try:
        result = run_diff(
            get_dialect(snowflake_url, side="A"), open_duckdb(duck, "B"), t, "main.t"
        )
    finally:
        _sf_exec(
            snowflake_url,
            f'alter user "{me}" unset {", ".join(names)}',
            f"drop table if exists {t}",
        )
    assert found(result) == [(3, "different", ("s",))]


# ================================================================ BigQuery


def test_bigquery_case_insensitive_collations(bigquery_url):
    """A table whose DEFAULT COLLATE is 'und:ci', and a column collated
    'und:ci': string keys 'a'/'A' stay two keys, case-only and ß/ss value
    changes are found, and chr(31) is escaped under the collation. Live, four
    tiny tables, dropped afterwards."""
    from google.cloud import bigquery

    from parity.dialects.bigquery_dialect import parse_bigquery_url

    project, dataset, location = parse_bigquery_url(bigquery_url)
    client = bigquery.Client(project=project, location=location)
    names = [f"rtg_ci_{x}_{RUN}" for x in "abcd"]

    def q(t: str) -> str:
        """The fully qualified, quoted table name."""
        return f"`{project}`.`{dataset}`.`{t}`"

    ta, tb, tc, td = names
    for sql in (
        f"create table {q(ta)} (k string, v string, n int64) default collate 'und:ci'",
        f"create table {q(tb)} (k string, v string, n int64) default collate 'und:ci'",
        f"insert into {q(ta)} values ('a','x',1), ('A','y',2), ('b','p\\x1fq',3), ('c','Z',4)",
        f"insert into {q(tb)} values ('a','x',1), ('A','Y',2), ('b','p\\x1fq',3), ('c','z',4)",
        f"create table {q(tc)} (id int64, v string collate 'und:ci')",
        f"create table {q(td)} (id int64, v string collate 'und:ci')",
        f"insert into {q(tc)} values (1, 'abc'), (2, 'ß'), (3, 'x')",
        f"insert into {q(td)} values (1, 'ABC'), (2, 'ss'), (3, 'x')",
    ):
        client.query_and_wait(sql)
    try:
        r1 = run_diff(
            get_dialect(bigquery_url, side="A"),
            get_dialect(bigquery_url, side="B"),
            ta,
            tb,
            key="k",
        )
        r2 = run_diff(
            get_dialect(bigquery_url, side="A"),
            get_dialect(bigquery_url, side="B"),
            tc,
            td,
        )
    finally:
        for t in names:
            client.delete_table(f"{project}.{dataset}.{t}", not_found_ok=True)
        client.close()
    assert found(r1) == [("A", "different", ("v",)), ("c", "different", ("v",))]
    assert found(r2) == [(1, "different", ("v",)), (2, "different", ("v",))]


# ================================================================== DuckDB


@pytest.mark.parametrize("collation", ["nocase", "noaccent", "nfc", "nocase.noaccent"])
def test_duckdb_column_collations(tmp_path, collation):
    """Collated key and value columns in the file itself (DuckDB keeps no
    session settings in a file, so this is what a database can carry): keys
    'a' and 'A' are two keys, and case, accent and normalisation changes in
    values are found."""
    rows_a = "('a', 'x', 1), ('A', 'y', 2), ('é', 'e\u0301', 3), ('e', 'p', 4)"
    rows_b = "('a', 'x', 1), ('A', 'Y', 2), ('é', 'é', 3), ('e', 'p', 4)"
    ddl = f"create table t (k varchar collate {collation}, v varchar collate {collation}, n int)"
    pa = duck_file(tmp_path, "ca", ddl, f"insert into t values {rows_a}")
    pb = duck_file(tmp_path, "cb", ddl, f"insert into t values {rows_b}")
    result = run_diff(
        open_duckdb(pa, "A"), open_duckdb(pb, "B"), "main.t", "main.t", key="k"
    )
    assert found(result) == [("A", "different", ("v",)), ("é", "different", ("v",))]
