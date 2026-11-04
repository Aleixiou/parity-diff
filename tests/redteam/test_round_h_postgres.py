"""Round H red team: PostgreSQL.

Every test plants a difference (or asserts a refusal). Confirmed bugs are
`xfail(strict=True)` with an `RT-HP<n>` id, so they turn green when fixed.

Endpoints (each skips cleanly when nothing listens):

- `PARITY_RTH_PG_ADMIN`: a PostgreSQL 18 where the login is superuser
  (roles, grants, RLS), default the disposable container `rth-pg-18`:
      docker run -d --name rth-pg-18 -e POSTGRES_USER=rth -e POSTGRES_PASSWORD=rth \\
          -e POSTGRES_DB=rth -p 15618:5432 postgres:18-alpine
- `PARITY_RTH_PG11`: PostgreSQL 11 (`rth-pg-11`, port 15611), the last
  release before shortest-repr float output.
- `PARITY_RTH_PG95`: PostgreSQL 9.5 (`rth-pg-95`, port 15695), must be refused.
- `PARITY_TEST_PG` (conftest): the native server, for locale-dependent money.

The container passwords are throwaway values for those local containers only.
Everything created is prefixed `rth_postgres_` and dropped afterwards.
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import quote

import pytest

from parity.dialects.base import get_dialect
from parity.engine import diff

from .rtg_servers import pg_connect

ADMIN = os.environ.get("PARITY_RTH_PG_ADMIN", "postgres://rth:rth@127.0.0.1:15618/rth")
PG11 = os.environ.get("PARITY_RTH_PG11", "postgres://rth:rth@127.0.0.1:15611/rth")
PG95 = os.environ.get("PARITY_RTH_PG95", "postgres://rth:rth@127.0.0.1:15695/rth")

RUN = uuid.uuid4().hex[:6]


def _exec(url: str, *stmts: str) -> list:
    """Run statements autocommit; the rows of the last one that returns any."""
    conn = pg_connect(url)
    out: list = []
    try:
        for s in stmts:
            cur = conn.execute(s)
            if cur.description:
                out = cur.fetchall()
    finally:
        conn.close()
    return out


def _diff(url_a: str, ta: str, url_b: str, tb: str, key: str = "id", **kw):
    """Diff two tables, closing both dialects whatever happens."""
    a = get_dialect(url_a, side="A")
    try:
        b = get_dialect(url_b, side="B")
        try:
            return diff(a, b, ta, tb, key, **kw)
        finally:
            b.close()
    finally:
        a.close()


@pytest.fixture
def schema():
    """A fresh schema on the admin server, dropped afterwards."""
    name = f"rth_postgres_{RUN}_{uuid.uuid4().hex[:4]}"
    _exec(ADMIN, f"create schema {name}")
    yield name
    _exec(ADMIN, f"drop schema if exists {name} cascade")


def _role(name: str) -> str:
    """A login role on the admin server (recreated), and its URL."""
    _exec(
        ADMIN,
        f"drop owned by {name}"
        if _exec(ADMIN, f"select 1 from pg_roles where rolname = '{name}'")
        else "select 1",
    )
    _exec(
        ADMIN, f"drop role if exists {name}", f"create role {name} login password 'rth'"
    )
    return f"postgres://{name}:rth@127.0.0.1:15618/rth" if "15618" in ADMIN else ADMIN


# ------------------------------------------------------------- findings


def test_rt_hp1_column_grant_hides_a_differing_column(schema):
    """A column hidden by column-level grants is never silently left out."""
    role = _role(f"rth_postgres_cg_{RUN}")
    _exec(
        ADMIN,
        f"grant usage on schema {schema} to rth_postgres_cg_{RUN}",
        *[
            f"create table {schema}.{t} (id int primary key, x text, secret text)"
            for t in "ab"
        ],
        *[
            f"insert into {schema}.{t} select g, 'v'||g, 's'||g from generate_series(1,100) g"
            for t in "ab"
        ],
        f"update {schema}.b set secret = 'CHANGED' where id = 7",
        *[f"grant select (id, x) on {schema}.{t} to rth_postgres_cg_{RUN}" for t in "ab"],
    )
    try:
        r = _diff(role, f"{schema}.a", role, f"{schema}.b")
        # Either the difference is found, or the run must not read identical.
        assert [d.key for d in r.diffs] == [7] or (
            not r.identical
            and any("secret" in w for w in r.warnings + r.schema_differences)
        )
    finally:
        _exec(
            ADMIN,
            f"drop owned by rth_postgres_cg_{RUN}",
            f"drop role rth_postgres_cg_{RUN}",
        )


@pytest.mark.parametrize("kind", ["security_invoker", "owner_subject_to_rls"])
def test_rt_hp4_view_over_rls_table_is_not_silent(schema, kind):
    """A view over an RLS table draws the row-level security caveat."""
    reader = f"rth_postgres_vr_{RUN}"
    owner = f"rth_postgres_vo_{RUN}"
    url = _role(reader)
    _exec(ADMIN, f"drop role if exists {owner}", f"create role {owner}")
    try:
        _exec(
            ADMIN,
            f"grant usage on schema {schema} to {reader}, {owner}",
            *[
                f"create table {schema}.{t} (id int primary key, x text, tenant text)"
                for t in "ab"
            ],
            f"insert into {schema}.a select g, 'v'||g, case when g % 2 = 0 then 'mine' "
            "else 'other' end from generate_series(1,100) g",
            f"insert into {schema}.b select * from {schema}.a",
            f"update {schema}.b set x = 'CHANGED' where id = 7",  # a hidden row
            *[f"alter table {schema}.{t} enable row level security" for t in "ab"],
            *[f"create policy p on {schema}.{t} using (tenant = 'mine')" for t in "ab"],
            *[f"grant select on {schema}.{t} to {reader}, {owner}" for t in "ab"],
        )
        for t in "ab":
            if kind == "security_invoker":
                _exec(
                    ADMIN,
                    f"create view {schema}.v{t} with (security_invoker = true) "
                    f"as select * from {schema}.{t}",
                )
            else:
                _exec(
                    ADMIN,
                    f"create view {schema}.v{t} as select * from {schema}.{t}",
                    f"alter view {schema}.v{t} owner to {owner}",
                )
            _exec(ADMIN, f"grant select on {schema}.v{t} to {reader}")
        r = _diff(url, f"{schema}.va", url, f"{schema}.vb")
        assert [d.key for d in r.diffs] == [7] or any(
            "row-level security" in w for w in r.warnings
        )
    finally:
        _exec(
            ADMIN,
            f"drop schema if exists {schema} cascade",
            f"drop owned by {reader}",
            f"drop role if exists {reader}",
            f"drop owned by {owner}",
            f"drop role if exists {owner}",
        )


def test_rt_hp3_date_past_timestamp_range_is_compared(schema):
    """A DATE past the timestamp range is compared, not a crash."""
    for t in "ab":
        _exec(
            ADMIN,
            f"create table {schema}.{t} (id int primary key, v date)",
            f"insert into {schema}.{t} values (1, '2024-01-01'), (2, '5874897-12-31')",
        )
    _exec(ADMIN, f"update {schema}.b set v = '5874897-12-30' where id = 2")
    r = _diff(ADMIN, f"{schema}.a", ADMIN, f"{schema}.b")
    assert [d.key for d in r.diffs] == [2]


@pytest.mark.parametrize(
    ("typ", "v1", "v2"),
    [
        ("point", "point(0.1, 0)", "point(0.10000000000000002, 0)"),
        ("circle", "circle(point(0,0), 0.1)", "circle(point(0,0), 0.10000000000000002)"),
        ("{s}.pr", "row(0.1, 1)", "row(0.10000000000000002, 1)"),
        ("{s}.pr", "row(0, 0.1)", "row(0, 0.10000001)"),
    ],
)
def test_rt_hp5_pg11_floats_inside_unmapped_types(typ, v1, v2):
    """On PostgreSQL 11 floats inside unmapped types are told apart."""
    s = f"rth_postgres_{RUN}_f11"
    _exec(
        PG11,
        f"drop schema if exists {s} cascade",
        f"create schema {s}",
        f"create type {s}.pr as (x float8, y float4)",
    )
    try:
        t = typ.format(s=s)
        for tb, v in (("a", v1), ("b", v2)):
            _exec(
                PG11,
                f"create table {s}.{tb} (id int primary key, v {t})",
                f"insert into {s}.{tb} values (1, {v1}), (2, {v})",
            )
        r = _diff(PG11, f"{s}.a", PG11, f"{s}.b")
        assert [d.key for d in r.diffs] == [2]
    finally:
        _exec(PG11, f"drop schema if exists {s} cascade")


def test_pg18_floats_inside_unmapped_types_are_found(schema):
    """The RT-HP5 values on a 12+ server, where the shortest repr tells them apart."""
    _exec(ADMIN, f"create type {schema}.pr as (x float8, y float4)")
    for tb, v in (("a", "row(0.1, 1)"), ("b", "row(0.10000000000000002, 1)")):
        _exec(
            ADMIN,
            f"create table {schema}.{tb} (id int primary key, v {schema}.pr)",
            f"insert into {schema}.{tb} values (1, row(0.1, 1)), (2, {v})",
        )
    r = _diff(ADMIN, f"{schema}.a", ADMIN, f"{schema}.b")
    assert [d.key for d in r.diffs] == [2]


def test_rt_hp6_where_with_set_config_cannot_undo_a_pin(schema):
    """A --where calling set_config() is refused or cannot move the verdict."""
    for t in "ab":
        _exec(ADMIN, f"create table {schema}.{t} (id int primary key, f float8)")
    _exec(
        ADMIN,
        f"insert into {schema}.a values (1, 0.1), (2, 1e15 + 0.5)",
        f"insert into {schema}.b values (1, 0.1), (2, 1e15)",
    )
    w = "set_config('extra_float_digits', '-15', false) is not null"
    try:
        r = _diff(ADMIN, f"{schema}.a", ADMIN, f"{schema}.b", where_a=w, where_b=w)
    except ValueError as exc:  # refusing such a filter is a fix too
        assert "row filter" in str(exc)
        return
    assert [d.key for d in r.diffs] == [2]


@pytest.mark.parametrize(
    "w",
    [
        "pg_catalog.set_config('extra_float_digits', '-15', false) is not null",
        "\"set_config\"('extra_float_digits', '-15', false) is not null",
        "U&\"\\0073et_config\"('extra_float_digits', '-15', false) is not null",
        "pg_sleep(1) is not null",
        "pg_advisory_lock(1) is not null",
        "query_to_xml('select 1', true, true, '') is not null",
    ],
)
def test_rt_hp6_session_changing_filters_are_refused(schema, w):
    """Filters calling session-changing functions are refused, however spelled."""
    _exec(ADMIN, f"create table {schema}.a (id int primary key)")
    with pytest.raises(ValueError, match="row filter"):
        _diff(ADMIN, f"{schema}.a", ADMIN, f"{schema}.a", where_a=w, where_b=w)


def test_rt_hp6_set_config_out_of_sight_is_never_identical(schema):
    """A user function - which no name check can see into - that rewrites a
    pin from inside the filter: the post-walk check makes it unexplained."""
    for t in "ab":
        _exec(ADMIN, f"create table {schema}.{t} (id int primary key, f float8)")
    _exec(
        ADMIN,
        f"insert into {schema}.a values (1, 0.1), (2, 1e15 + 0.5)",
        f"insert into {schema}.b values (1, 0.1), (2, 1e15)",
        f"create function {schema}.f() returns bool language sql as "
        "$$ select set_config('extra_float_digits', '-15', false) is not null $$",
    )
    w = f"{schema}.f()"
    r = _diff(ADMIN, f"{schema}.a", ADMIN, f"{schema}.b", where_a=w, where_b=w)
    assert not r.identical
    assert any("extra_float_digits" in u for u in r.unexplained)
    # Without it, the planted difference is found and nothing is unexplained.
    r = _diff(ADMIN, f"{schema}.a", ADMIN, f"{schema}.b")
    assert [d.key for d in r.diffs] == [2] and not r.unexplained


def test_rt_hp1_excluded_unreadable_column_is_a_note(schema):
    """An unreadable column set aside with --exclude is a note, not a difference."""
    role = _role(f"rth_postgres_cx_{RUN}")
    _exec(
        ADMIN,
        f"grant usage on schema {schema} to rth_postgres_cx_{RUN}",
        *[
            f"create table {schema}.{t} (id int primary key, x text, secret text)"
            for t in "ab"
        ],
        *[
            f"insert into {schema}.{t} select g, 'v'||g, 's' from generate_series(1,50) g"
            for t in "ab"
        ],
        f"update {schema}.b set x = 'D' where id = 3",
        *[f"grant select (id, x) on {schema}.{t} to rth_postgres_cx_{RUN}" for t in "ab"],
    )
    try:
        r = _diff(role, f"{schema}.a", role, f"{schema}.b")
        assert [d.key for d in r.diffs] == [3]
        assert any("cannot read ['secret']" in s for s in r.schema_differences)
        r = _diff(role, f"{schema}.a", role, f"{schema}.b", exclude=["secret"])
        assert [d.key for d in r.diffs] == [3] and not r.schema_differences
        assert any("secret" in w for w in r.warnings)
    finally:
        _exec(
            ADMIN,
            f"drop owned by rth_postgres_cx_{RUN}",
            f"drop role rth_postgres_cx_{RUN}",
        )


def test_inheritance_children_draw_a_caveat(schema):
    """A parent table's inheritance children are named in a caveat."""
    _exec(
        ADMIN,
        f"create table {schema}.p (id int primary key, x text)",
        f"create table {schema}.ch (check (id > 100)) inherits ({schema}.p)",
        f"create table {schema}.b (id int primary key, x text)",
        f"insert into {schema}.p values (1, 'a')",
        f"insert into {schema}.ch values (101, 'b')",
        f"insert into {schema}.b values (1, 'a')",
    )
    r = _diff(ADMIN, f"{schema}.p", ADMIN, f"{schema}.b")
    assert [(d.key, d.kind) for d in r.diffs] == [(101, "only_in_a")]
    assert any("inheritance children" in w and ".ch" in w for w in r.warnings)


def test_where_and_map_postgres_quoting(schema):
    """E'', U&'', doubled quotes and casts work; $$ and backslash-quote are refused;
    a filter cannot close the parentheses it is wrapped in."""
    for t in "ab":
        _exec(
            ADMIN,
            f'create table {schema}.{t} (id int primary key, "Name" text, amt numeric)',
            f"insert into {schema}.{t} select g, 'n'||g, g * 1.5 from generate_series(1, 1000) g",
        )
    _exec(ADMIN, f"update {schema}.b set amt = -1 where id in (10, 900)")
    a, b = f"{schema}.a", f"{schema}.b"
    for w, keys in [
        ("id < 500", [10]),
        ("E'a\\\\b' <> \"Name\"", [10, 900]),
        ("U&'\\0061' <> \"Name\"", [10, 900]),
        ("id = any('{10,900}'::int[])", [10, 900]),
        ("'(' = '(' and id > 500", [900]),
    ]:
        r = _diff(ADMIN, a, ADMIN, b, where_a=w, where_b=w)
        assert sorted(d.key for d in r.diffs) == keys, w
    for w in [
        '"Name" <> $$x$$',
        '"Name" <> $t$x$t$',
        "true) or (true",
        "id < 5 -- c",
        "E'\\'' <> \"Name\"",
        "id < 5; select 1",
    ]:
        with pytest.raises(ValueError, match="row filter"):
            _diff(ADMIN, a, ADMIN, b, where_a=w, where_b=w)
    _exec(
        ADMIN,
        f"create table {schema}.m (id int primary key, nm text, Amount numeric)",
        f"insert into {schema}.m select * from {b}",
    )
    r = _diff(ADMIN, a, ADMIN, f"{schema}.m", column_map={"Name": "nm", "amt": "AMOUNT"})
    assert sorted(d.key for d in r.diffs) == [10, 900] and not r.schema_differences
    with pytest.raises(ValueError, match="--map"):
        _diff(ADMIN, a, ADMIN, f"{schema}.m", column_map={"Name": "nm", "amt": "nm"})


def test_rt_hp7_unreachable_server_fails_within_a_minute():
    """An unreachable server fails within a minute, naming the side."""
    import subprocess
    import sys

    cmd = [
        sys.executable,
        "-c",
        "import sys; from parity.cli import main; sys.exit(main())",
        "diff",
        "--a",
        "postgres://rth:x@127.0.0.1:1/rth",
        "--a-table",
        "t",
        "--b",
        "duckdb:///:memory:",
        "--b-table",
        "t",
        "--key",
        "id",
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)  # noqa: S603 - our own CLI
    except subprocess.TimeoutExpired:
        pytest.fail("no answer after 60 s")
    assert p.returncode == 2 and "side A" in p.stderr


def test_rt_hp8_composite_key_with_a_null_field_is_a_key(schema):
    """row(1, NULL) is a valid composite key."""
    _exec(
        ADMIN,
        f"create type {schema}.c as (a int, b text)",
        *[f"create table {schema}.{t} (k {schema}.c, v int)" for t in "ab"],
        f"insert into {schema}.a values (row(1, NULL), 1), (row(1, ''), 2)",
        f"insert into {schema}.b values (row(1, NULL), 1)",
    )
    r = _diff(ADMIN, f"{schema}.a", ADMIN, f"{schema}.b", key="k")
    assert [(d.key, d.kind) for d in r.diffs] == [('(1,"")', "only_in_a")]


def test_rt_hp9_untrusted_type_is_compared_by_its_send_bytes(schema, monkeypatch):
    """A base type outside the trusted list, with a send function, compares by
    its bytes too (cube, taken off the list for the test); nested in an array
    it is refused, naming the column."""
    from parity.dialects import postgres_dialect

    monkeypatch.setattr(
        postgres_dialect,
        "TRUSTED_EXTENSION_TYPES",
        postgres_dialect.TRUSTED_EXTENSION_TYPES - {"cube"},
    )
    _exec(
        ADMIN,
        "create extension if not exists cube",
        *[
            f"create table {schema}.{t} (id int primary key, v cube, w text)"
            for t in "ab"
        ],
        *[
            f"insert into {schema}.{t} values (1, cube(1.5), 'x'), (2, cube(2), 'x'), "
            "(3, NULL, 'x')"
            for t in "ab"
        ],
        f"update {schema}.b set v = cube(2.0000001) where id = 2",
        f"update {schema}.b set w = 'y' where id = 1",
    )
    r = _diff(ADMIN, f"{schema}.a", ADMIN, f"{schema}.b")
    assert sorted((d.key, tuple(d.columns)) for d in r.diffs) == [
        (1, ("w",)),
        (2, ("v",)),
    ]
    _exec(
        ADMIN,
        *[f"create table {schema}.n{t} (id int primary key, v cube[])" for t in "ab"],
        *[f"insert into {schema}.n{t} values (1, array[cube(1)])" for t in "ab"],
    )
    with pytest.raises(ValueError, match=r"column 'v' holds .*cube.*--exclude"):
        _diff(ADMIN, f"{schema}.na", ADMIN, f"{schema}.nb")
    r = _diff(ADMIN, f"{schema}.na", ADMIN, f"{schema}.nb", exclude=["v"])
    assert r.identical


def test_rt_hp9_unmapped_type_whose_text_is_lossy(schema):
    """Two seg values that print alike are found or refused, never equal."""
    _exec(
        ADMIN,
        "create extension if not exists seg",
        *[f"create table {schema}.{t} (id int primary key, v seg)" for t in "ab"],
        f"insert into {schema}.a values (1, '1.5'), (2, '123456789')",
        f"insert into {schema}.b values (1, '1.5'), (2, '123456788')",
    )
    assert not _exec(
        ADMIN,
        f"select a.v = b.v from {schema}.a a join {schema}.b b using (id) where id = 2",
    )[0][0]
    try:
        r = _diff(ADMIN, f"{schema}.a", ADMIN, f"{schema}.b")
    except ValueError as exc:  # refusing a type it cannot compare exactly is a fix too
        assert "seg" in str(exc) and "--exclude" in str(exc)
        return
    assert [d.key for d in r.diffs] == [2]


# ------------------------------------------------- held (regression tests)


def test_hostile_alter_role_set_defaults_do_not_move_the_verdict(schema):
    """Every rendering setting at a hostile value, stored the way a real login
    carries them (ALTER ROLE ... SET), and the planted values are still found -
    including the ones each setting would hide - at every sample size."""
    role = f"rth_postgres_hr_{RUN}"
    url = _role(role)
    s = schema
    _exec(
        ADMIN,
        f"grant usage on schema {s} to {role}",
        *[
            f"create table {s}.{t} (id int primary key, f float8, ts timestamptz, d date, "
            "i interval, b bytea, t text, m money)"
            for t in "ab"
        ],
        *[
            f"insert into {s}.{t} select g, g * 0.1, timestamptz '2024-01-01' + g * interval '1 h', "
            "date '2024-01-01' + g, g * interval '1 day', '\\x00ff', 'N', g "
            "from generate_series(1, 2000) g"
            for t in "ab"
        ],
        f"update {s}.b set f = 1e15 + 0.5 where id = 1",
        f"update {s}.a set f = 1e15 where id = 1",
        f"update {s}.b set ts = ts + interval '1 h' where id = 2",
        f"update {s}.b set i = interval '1 mon' where id = 3",
        f"update {s}.a set i = interval '30 days' where id = 3",
        f"update {s}.b set t = NULL where id = 4",
        *[f"grant select on {s}.{t} to {role}" for t in "ab"],
    )
    for setting in [
        "extra_float_digits = -15",
        "IntervalStyle = sql_standard",
        "DateStyle = 'German, DMY'",
        "TimeZone = 'Asia/Kathmandu'",
        "bytea_output = escape",
        "standard_conforming_strings = off",
        "search_path = pg_temp, public",
        "idle_in_transaction_session_timeout = '1s'",
        "quote_all_identifiers = on",
        "default_transaction_isolation = serializable",
    ]:
        _exec(ADMIN, f"alter role {role} set {setting}")
    try:
        for sample in (0, 1, 50000):
            r = _diff(url, f"{s}.a", url, f"{s}.b", verify_sample=sample)
            assert sorted((d.key, tuple(d.columns)) for d in r.diffs) == [
                (1, ("f",)),
                (2, ("ts",)),
                (3, ("i",)),
                (4, ("t",)),
            ], sample
    finally:
        _exec(ADMIN, f"drop owned by {role}", f"drop role {role}")


def test_unmapped_and_temporal_keys(schema):
    """Range, enum and composite keys stay distinct; a PostgreSQL 18 temporal
    primary key (WITHOUT OVERLAPS) finds the planted period; two spellings of
    one discrete range are one key, so a duplicate."""
    s = schema
    _exec(
        ADMIN,
        "create extension if not exists btree_gist",
        f"create type {s}.e as enum ('x', 'X')",
        f"create type {s}.c as (a int, b text)",
    )
    for i, (t, k1, k2) in enumerate(
        [
            ("int4range", "'[1,5)'", "'[1,6)'"),
            ("tstzrange", "'[2024-01-01,2024-02-01)'", "'[2024-01-01,2024-02-02)'"),
            (f"{s}.e", "'x'", "'X'"),
            (f"{s}.c", "row(1,'a b')", "row(1,'a  b')"),
        ]
    ):
        _exec(
            ADMIN,
            f"create table {s}.a{i} (k {t}, v int)",
            f"create table {s}.b{i} (k {t}, v int)",
            f"insert into {s}.a{i} values ({k1}, 1), ({k2}, 2)",
            f"insert into {s}.b{i} values ({k1}, 1)",
        )
        r = _diff(ADMIN, f"{s}.a{i}", ADMIN, f"{s}.b{i}", key="k")
        assert [d.kind for d in r.diffs] == ["only_in_a"], t
    _exec(
        ADMIN,
        f"create table {s}.dup (k int4range, v int)",
        f"insert into {s}.dup values ('[1,5)', 1), ('(0,4]', 2)",
    )
    with pytest.raises(ValueError, match="not unique"):
        _diff(ADMIN, f"{s}.dup", ADMIN, f"{s}.dup", key="k")
    _exec(
        ADMIN,
        *[
            f"create table {s}.t{t} (id int, valid tstzrange, x text, "
            "primary key (id, valid without overlaps))"
            for t in "ab"
        ],
        *[
            f"insert into {s}.t{t} values (1, '[2024-01-01,2024-02-01)', 'a'), "
            "(1, '[2024-02-01,2024-03-01)', 'b'), (2, '[2024-01-01,)', 'c')"
            for t in "ab"
        ],
        f"update {s}.tb set x = 'B' where id = 1 and lower(valid) = '2024-02-01'",
    )
    r = _diff(ADMIN, f"{s}.ta", ADMIN, f"{s}.tb", key=["id", "valid"])
    assert [(d.kind, d.columns) for d in r.diffs] == [("different", ["x"])]
    assert "2024-02-01" in str(r.diffs[0].key)


def test_foreign_table_and_bisection_extremes(schema):
    """A postgres_fdw foreign table reads like the table it mirrors (the remote
    session's own settings do not reach the canonical text); every bisection
    extreme finds the planted 1 us change and ignores a change below the scale."""
    s = schema
    srv = f"rth_postgres_loop_{RUN}"
    _exec(
        ADMIN,
        "create extension if not exists postgres_fdw",
        f"create server {srv} foreign data wrapper postgres_fdw "
        "options (host '127.0.0.1', port '5432', dbname 'rth')",
        f"create user mapping for current_user server {srv} options (user 'rth', password 'rth')",
        *[
            f"create table {s}.{t} (id int primary key, x text, f float8, ts timestamptz, "
            "i interval, n numeric)"
            for t in "ab"
        ],
        *[
            f"insert into {s}.{t} select g, 'v'||g, g * 0.1 + 1e-9, "
            "timestamptz '2024-06-01 12:00+02' + g * interval '1 min', "
            "g * interval '1 mon 1 day', g / 7.0 from generate_series(1, 5000) g"
            for t in "ab"
        ],
        f"update {s}.b set f = f + 1e-15 * f where id = 10",
        f"update {s}.b set ts = ts + interval '1 us' where id = 20",
        f"create foreign table {s}.fb (id int, x text, f float8, ts timestamptz, i interval, "
        f"n numeric) server {srv} options (schema_name '{s}', table_name 'b')",
    )
    try:
        r = _diff(ADMIN, f"{s}.a", ADMIN, f"{s}.fb")
        assert [(d.key, d.columns) for d in r.diffs] == [(20, ["ts"])]
        for factor, threshold in [(2, 1), (1000, 1), (2, 100000)]:
            r = _diff(
                ADMIN,
                f"{s}.a",
                ADMIN,
                f"{s}.b",
                bisection_factor=factor,
                threshold=threshold,
            )
            assert [(d.key, d.columns) for d in r.diffs] == [(20, ["ts"])]
    finally:
        _exec(ADMIN, f"drop server if exists {srv} cascade")


@pytest.mark.parametrize(
    ("typ", "k1", "k2"),
    [
        ("text", "'a'", "'a '"),
        ("text", "'a'", "'A'"),
        ("text", "E'\\u00e9'", "E'e\\u0301'"),
        ("text collate {s}.ci", "'a'", "'A'"),
        ("citext", "'a'", "'A'"),
        ("varchar(5)", "''", "' '"),
        (
            "uuid",
            "'00000000-0000-0000-0000-000000000000'",
            "'ffffffff-ffff-ffff-ffff-ffffffffffff'",
        ),
        ("numeric", "1.5", "1.50000000001"),
        ("numeric", "'NaN'", "'Infinity'"),
        ("money", "1", "1.01"),
        ("date", "'infinity'", "'-infinity'"),
        ("timestamptz", "'2024-01-01 00:00:00+00'", "'2024-01-01 00:00:00.000001+00'"),
        ("timestamp", "'0044-03-15 BC'", "'0044-03-15'"),
        ("time", "'24:00'", "'00:00'"),
        ("interval", "'1 mon'", "'30 days'"),
        ("bytea", "'\\x00'", "'\\x0000'"),
        ("inet", "'10.0.0.1'", "'10.0.0.1/24'"),
        ("oid", "0", "4294967295"),
        ("bigint", "-9223372036854775808", "9223372036854775807"),
        ("jsonb", "'1'", "'1.0'"),
    ],
)
def test_distinct_keys_of_every_type_stay_distinct(schema, typ, k1, k2):
    """A holds k1 and k2, B only k1: exactly k2 is only_in_a."""
    _exec(
        ADMIN,
        "create extension if not exists citext",
        f"create collation {schema}.ci (provider = icu, locale = 'und-u-ks-level2', "
        "deterministic = false)",
    )
    t = typ.format(s=schema)
    _exec(
        ADMIN,
        f"create table {schema}.a (k {t}, v int)",
        f"create table {schema}.b (k {t}, v int)",
        f"insert into {schema}.a values ({k1}, 1), ({k2}, 2)",
        f"insert into {schema}.b values ({k1}, 1)",
    )
    r = _diff(ADMIN, f"{schema}.a", ADMIN, f"{schema}.b", key="k")
    assert [d.kind for d in r.diffs] == ["only_in_a"]


#: A hot standby of ADMIN (container `rth-pg-18r`, `pg_basebackup -R`, port 15619).
STANDBY = os.environ.get(
    "PARITY_RTH_PG_STANDBY", "postgres://rth:rth@127.0.0.1:15619/rth"
)


def test_hot_standby_reads_one_snapshot_while_the_primary_writes(schema):
    """On a read replica the walk keeps one snapshot even while the primary's
    writes and a VACUUM replay underneath it; the planted row is found."""
    import time

    conn = pg_connect(STANDBY)
    try:
        if not conn.execute("select pg_is_in_recovery()").fetchone()[0]:
            pytest.skip("not a standby")
    finally:
        conn.close()
    s = schema
    _exec(
        ADMIN,
        *[f"create table {s}.{t} (id int primary key, x text)" for t in "ab"],
        *[
            f"insert into {s}.{t} select g, 'v'||g from generate_series(1, 50000) g"
            for t in "ab"
        ],
        f"update {s}.b set x = 'D' where id = 777",
    )
    for _ in range(60):
        try:
            if _exec(STANDBY, f"select count(*) from {s}.b where x = 'D'")[0][0] == 1:
                break
        except Exception:  # noqa: S110 - not replayed yet
            pass
        time.sleep(1)
    a, b = get_dialect(STANDBY, side="A"), get_dialect(STANDBY, side="B")
    original, fired = b.segment_checksums, []

    def wrapped(*args, **kw):
        """Run the real checksum query, then write to the table once."""
        out = original(*args, **kw)
        if not fired:
            fired.append(True)
            _exec(
                ADMIN,
                f"update {s}.b set x = 'LIVE' where id = 2",
                f"delete from {s}.b where id between 100 and 20000",
                f"vacuum {s}.b",
            )
            time.sleep(3)  # let it replay
        return out

    b.segment_checksums = wrapped
    try:
        r = diff(a, b, f"{s}.a", f"{s}.b", "id")
    finally:
        a.close()
        b.close()
    assert fired and [(d.key, d.kind) for d in r.diffs] == [(777, "different")]
    assert not r.unexplained


def test_wide_table_odd_identifiers_and_a_toasted_value(schema):
    """600 columns, odd identifiers and a 10 MB value find their planted changes."""
    s = schema
    cols = ", ".join(f"c{i} int" for i in range(600))
    names = ", ".join(f"c{i}" for i in range(600))
    _exec(
        ADMIN,
        *[f"create table {s}.w{t} (id int primary key, {cols})" for t in "ab"],
        *[
            f"insert into {s}.w{t} (id, {names}) select g, {', '.join('g' for _ in range(600))} "
            "from generate_series(1, 50) g"
            for t in "ab"
        ],
        f"update {s}.wb set c599 = -1 where id = 25",
        f"update {s}.wb set c0 = -1 where id = 26",
    )
    r = _diff(ADMIN, f"{s}.wa", ADMIN, f"{s}.wb")
    assert sorted((d.key, tuple(d.columns)) for d in r.diffs) == [
        (25, ("c599",)),
        (26, ("c0",)),
    ]
    odd = f'{s} Ödd"q'
    qodd = '"' + odd.replace('"', '""') + '"'
    long = "ü" * 31
    _exec(
        ADMIN,
        f"create schema {qodd}",
        *[
            f'create table {qodd}."T {t}" ("ID" int primary key, "select ""x""\'y" text, '
            f'"{long}" text, "order" text)'
            for t in "ab"
        ],
        *[
            f"insert into {qodd}.\"T {t}\" values (1, 'a', 'b', 'c'), (2, 'a', 'b', 'c')"
            for t in "ab"
        ],
        f'update {qodd}."T b" set "order" = \'X\' where "ID" = 2',
    )
    try:
        r = _diff(ADMIN, f"{odd}.T a", ADMIN, f"{odd}.T b", key="id")
        assert [(d.key, d.columns) for d in r.diffs] == [(2, ["order"])]
    finally:
        _exec(ADMIN, f"drop schema {qodd} cascade")
    _exec(
        ADMIN,
        *[f"create table {s}.h{t} (id int primary key, v text)" for t in "ab"],
        *[
            f"insert into {s}.h{t} values (1, repeat('abcdefgh', 1250000)), (2, 'x')"
            for t in "ab"
        ],
        f"update {s}.hb set v = overlay(v placing 'Z' from 9999990) where id = 1",
    )
    r = _diff(ADMIN, f"{s}.ha", ADMIN, f"{s}.hb")
    assert [(d.key, d.columns) for d in r.diffs] == [(1, ["v"])] and not r.unexplained


def test_partitions_generated_columns_and_missing_grants(schema):
    """Partitions, generated columns and a missing grant behave."""
    s = schema
    _exec(
        ADMIN,
        f"create table {s}.pa (id int, r text, x text, primary key (id, r)) partition by list (r)",
        f"create table {s}.pa_e partition of {s}.pa for values in ('eu')",
        f"create table {s}.pa_u partition of {s}.pa for values in ('us')",
        f"create table {s}.pb (id int, r text, x text, primary key (id, r))",
        f"insert into {s}.pa select g, case when g % 2 = 0 then 'eu' else 'us' end, 'v' "
        "from generate_series(1, 1000) g",
        f"insert into {s}.pb select * from {s}.pa",
        f"update {s}.pb set x = 'D' where id = 501",
        f"create table {s}.ga (id int primary key, a int, s int generated always as (a * 2) stored, "
        "v int generated always as (a * 3) virtual)",
        f"create unlogged table {s}.gb (id int primary key, a int, s int, v int)",
        f"insert into {s}.ga (id, a) select g, g from generate_series(1, 100) g",
        f"insert into {s}.gb select id, a, s, v from {s}.ga",
        f"update {s}.gb set v = 0 where id = 50",
    )
    r = _diff(ADMIN, f"{s}.pa", ADMIN, f"{s}.pb", key=["id", "r"])
    assert [(d.key, d.kind) for d in r.diffs] == [("501\x1fus", "different")]
    r = _diff(ADMIN, f"{s}.pa_u", ADMIN, f"{s}.pb", key=["id", "r"], where_b="r = 'us'")
    assert [d.key for d in r.diffs] == ["501\x1fus"]
    r = _diff(ADMIN, f"{s}.ga", ADMIN, f"{s}.gb")
    assert [(d.key, d.columns) for d in r.diffs] == [(50, ["v"])]
    url = _role(f"rth_postgres_np_{RUN}")
    try:
        with pytest.raises(
            ValueError, match=r"side A.*exists but this role cannot read it"
        ):
            _diff(url, f"{s}.pa", ADMIN, f"{s}.pb", key=["id", "r"])
    finally:
        _exec(ADMIN, f"drop role rth_postgres_np_{RUN}")


@pytest.mark.parametrize(
    "url",
    [
        "postgres://rth:S3cr3tPw!@127.0.0.1:15618/rth",
        "POSTGRES://rth:S3cr3t Pw@127.0.0.1:15618/rth",
        "postgres://rth@127.0.0.1:15618/rth?password=S3cr3tPw!&sslmode=bogus",
    ],
)
def test_cli_connection_errors_never_print_the_password(url, schema):
    """Connection errors from the CLI never print the password."""
    import subprocess
    import sys

    pg_connect(ADMIN).close()
    cmd = [
        sys.executable,
        "-c",
        "import sys; from parity.cli import main; sys.exit(main())",
        "diff",
        "--a",
        url,
        "--a-table",
        f"{schema}.t",
        "--b",
        ADMIN,
        "--b-table",
        f"{schema}.t",
        "--key",
        "id",
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)  # noqa: S603 - our own CLI
    assert p.returncode == 2
    assert "side A" in p.stderr and "S3cr3t" not in p.stdout + p.stderr


def test_live_writer_mid_walk_is_invisible_then_seen(schema):
    """A writer mid-walk is invisible to the snapshot, then seen by a new run."""
    s = schema
    _exec(
        ADMIN,
        f"create table {s}.a (id bigint primary key, x text, n numeric(12,2))",
        f"insert into {s}.a select g, md5(g::text), g / 100.0 from generate_series(1, 200000) g",
        f"create table {s}.b as select * from {s}.a",
        f"alter table {s}.b add primary key (id)",
        f"update {s}.b set x = 'first' where id = 1",
        f"update {s}.b set n = n + 0.01 where id = 200000",
        f"insert into {s}.b values (9223372036854775807, 'max', 0)",
    )
    planted = [
        (1, "different"),
        (200000, "different"),
        (9223372036854775807, "only_in_b"),
    ]
    a, b = get_dialect(ADMIN, side="A"), get_dialect(ADMIN, side="B")
    original, fired = b.segment_checksums, []

    def wrapped(*args, **kw):
        """Run the real checksum query, then write to the table once."""
        out = original(*args, **kw)
        if not fired:
            fired.append(True)
            _exec(
                ADMIN,
                f"update {s}.b set x = 'LIVE' where id = 2",
                f"delete from {s}.b where id = 3",
                f"insert into {s}.b values (-5, 'new', 0)",
            )
        return out

    b.segment_checksums = wrapped
    try:
        r = diff(a, b, f"{s}.a", f"{s}.b", "id")
    finally:
        a.close()
        b.close()
    assert (
        fired
        and sorted((d.key, d.kind) for d in r.diffs) == planted
        and not r.unexplained
    )
    r = _diff(ADMIN, f"{s}.a", ADMIN, f"{s}.b")
    assert sorted((d.key, d.kind) for d in r.diffs) == sorted(
        [*planted, (-5, "only_in_b"), (2, "different"), (3, "only_in_a")]
    )
    r = _diff(ADMIN, f"{s}.a", ADMIN, f"{s}.b", max_diffs=2)
    assert r.truncated and not r.identical and len(r.diffs) == 2


@pytest.mark.parametrize("encoding", ["EUC_JP", "SQL_ASCII", "EUC_KR"])
def test_non_utf8_server_encodings_against_duckdb(encoding, tmp_path):
    """Text stored in a non-UTF-8 database matches DuckDB's UTF-8 text, the
    planted change (key 3) is found, and a value the encoding cannot hold is
    missing on that side, never silently equal."""
    import duckdb

    # Fullwidth tilde, parallel and minus sign are deliberate: EUC_JP maps them specially.
    vals = ["日本語", "ｶﾀｶﾅ", "①", "髙", "～", "∥", "−", "\\", "é", "abc"]  # noqa: RUF001
    path = (tmp_path / "enc.duckdb").as_posix()
    con = duckdb.connect(path)
    con.execute("create table t (id int primary key, v varchar)")
    for i, v in enumerate(vals, 1):
        con.execute("insert into t values (?, ?)", [i, v + ("X" if i == 3 else "")])
    con.close()
    db = f"rth_postgres_{encoding.lower()}_{RUN}"
    _exec(
        ADMIN,
        f"create database {db} encoding '{encoding}' lc_collate 'C' "
        "lc_ctype 'C' template template0",
    )
    url = ADMIN.rsplit("/", 1)[0] + "/" + db
    try:
        _exec(url, "create table t (id int primary key, v text)")
        stored = set()
        conn = pg_connect(url)
        try:
            conn.execute("set client_encoding = 'UTF8'")
            for i, v in enumerate(vals, 1):
                try:
                    conn.execute("insert into t values (%s, %s)", (i, v))
                    stored.add(i)
                except Exception:  # noqa: S110 - not representable in this encoding
                    pass
        finally:
            conn.close()
        assert 3 in stored
        r = _diff(url, "public.t", f"duckdb:///{path}", "main.t")
        expected = {(3, "different")} | {
            (i, "only_in_b") for i in range(1, 11) if i not in stored
        }
        assert {(d.key, d.kind) for d in r.diffs} == expected
        assert not r.unexplained
    finally:
        _exec(ADMIN, f"drop database if exists {db}")


def test_pg95_is_refused_naming_the_version():
    """PostgreSQL 9.5 is refused, naming the version."""
    pg_connect(PG95).close()
    with pytest.raises(ValueError, match=r"side A.*9\.5.*not supported"):
        get_dialect(PG95, side="A")


def test_gin_fuzzy_search_limit_under_a_filter_is_never_identical(schema):
    """A role default `gin_fuzzy_search_limit` makes GIN scans return a random
    subset; the per-level row-count check must refuse to call it identical."""
    for t in "ab":
        _exec(
            ADMIN,
            f"create table {schema}.{t} (id int primary key, tags int[], x text)",
            f"insert into {schema}.{t} select g, array[1, g % 7], 'v'||g "
            "from generate_series(1, 20000) g",
            f"create index on {schema}.{t} using gin (tags)",
            f"analyze {schema}.{t}",
        )
    _exec(ADMIN, f"update {schema}.b set x = 'CHANGED' where id = 5000")
    opts = quote("-c gin_fuzzy_search_limit=100 -c enable_seqscan=off")
    url = f"{ADMIN}?options={opts}"
    w = "tags @> '{1}'"
    r = _diff(
        url, f"{schema}.a", url, f"{schema}.b", where_a=w, where_b=w, verify_sample=0
    )
    assert not r.identical and r.unexplained


@pytest.mark.parametrize(
    ("typ", "v1", "v2"),
    [
        ("numeric", "'NaN'", "'Infinity'"),
        ("numeric", "'Infinity'", "'-Infinity'"),
        ("numeric[]", "'{NaN}'", "'{Infinity}'"),
        ('"char"', "E'\\\\201'::\"char\"", "E'\\\\202'::\"char\""),
        ("char(3)", "'a'", "'a\t'"),
        ("hstore", "'a=>NULL'", "'a=>\"NULL\"'"),
        ("int4range", "'empty'", "NULL"),
        (
            "tstzrange",
            "'[2024-01-01 00:00+00,2024-01-02 00:00+00)'",
            "'[2024-01-01 00:00+01,2024-01-02 00:00+00)'",
        ),
        ("xml", "'<a/>'", "'<a></a>'"),
        ("point", "'(0,0)'", "'(-0,0)'"),
        ("inet", "'::ffff:1.2.3.4'", "'1.2.3.4'"),
        ("jsonb", "'\"\\\\N\"'", "'null'"),
        ("interval", "'-1 mon 1 day'", "'1 mon -1 day'"),
        ("timetz", "'12:00:00-00:00:01'", "'12:00:00+00:00:01'"),
        ("timestamp", "'0001-01-01 00:00:00 BC'", "'0001-01-01 00:00:00'"),
        ("text[]", "array[E'\\\\,']", "array[E'\\\\', '']"),
        ("int[]", "'[2:3]={1,2}'", "'[3:4]={1,2}'"),
        ("varchar(3)[]", "'{a}'", "'{\"a \"}'"),
        ("timestamptz[]", "'{2024-01-01 00:00+00}'", "'{2024-01-01 00:00+01}'"),
        ("{s}.pair", "row('', NULL)", "row(NULL, NULL)"),
        ("text", "repeat('x', 3000000)", "repeat('x', 2999999) || 'y'"),
    ],
)
def test_distinct_values_of_exotic_types_are_found(schema, typ, v1, v2):
    """Two different values of each type: exactly the planted key differs."""
    _exec(
        ADMIN,
        "create extension if not exists hstore",
        f"create type {schema}.pair as (a text, b float8)",
    )
    t = typ.format(s=schema)
    _exec(
        ADMIN,
        f"create table {schema}.a (id int primary key, v {t})",
        f"create table {schema}.b (id int primary key, v {t})",
        f"insert into {schema}.a values (1, {v1}), (2, {v1})",
        f"insert into {schema}.b values (1, {v1}), (2, {v2})",
    )
    r = _diff(ADMIN, f"{schema}.a", ADMIN, f"{schema}.b")
    assert [d.key for d in r.diffs] == [2]
    assert not r.unexplained


def test_rt_hp2_money_in_a_zero_decimal_locale(pg_url, tmp_path):
    """Yen money is read at the login's own frac_digits."""
    import duckdb

    table = f"rth_postgres_money_{RUN}"
    conn = pg_connect(pg_url)
    try:
        locale = None
        for name in ("Japanese_Japan.932", "ja_JP.UTF-8", "ja_JP.utf8"):
            try:
                conn.execute(f"set lc_monetary = '{name}'")
                locale = name
                break
            except Exception:  # noqa: S112 - locale not installed here
                continue
        if (
            locale is None
            or conn.execute("select '1.5'::money::numeric").fetchone()[0] != 2
        ):
            pytest.skip("no 0-decimal monetary locale on this server")
        conn.execute(f"create table public.{table} (id int primary key, m money)")
        conn.execute(f"insert into public.{table} values (1, '12345'), (2, '100')")
    finally:
        conn.close()
    path = (tmp_path / "m.duckdb").as_posix()
    con = duckdb.connect(path)
    con.execute("create table m (id int primary key, m decimal(18,2))")
    con.execute("insert into m values (1, 12345), (2, 1.00)")  # key 2 really differs
    con.close()
    sep = "&" if "?" in pg_url else "?"
    url = f"{pg_url}{sep}options={quote(f'-c lc_monetary={locale}')}"
    try:
        r = _diff(url, f"public.{table}", f"duckdb:///{path}", "main.m")
        assert [d.key for d in r.diffs] == [2]
        assert any("lc_monetary" in w for w in r.warnings)
    finally:
        _exec(pg_url, f"drop table if exists public.{table}")
