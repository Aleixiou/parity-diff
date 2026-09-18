"""Real PostgreSQL against real DuckDB, end to end through the CLI and engine.

`test_encoding.py` proves the two engines render values identically and
`test_engine.py` proves the walker is correct in isolation. This file is the
one that would catch a mistake living in the seam between them - a dialect that
renders correctly but aggregates wrongly, a checksum that overflows, a bucket
expression that behaves differently in real SQL than in Python.

The data is identical *by construction*: one `generate_series` expression runs
verbatim on both engines. Any difference reported here is either one the test
planted or a bug.

Skips cleanly when no PostgreSQL is reachable.
"""

from __future__ import annotations

import json

import pytest
from conftest import PG_SCHEMA, duckdb_write, open_duckdb, open_pg

from parity.cli import EXIT_DIFFERENCES, EXIT_ERROR, EXIT_IDENTICAL, main
from parity.engine import diff

N = 20_000

#: Spelled identically on both engines. That is the point.
SELECT = """
select i::bigint                                             as id,
       (i % 97)::integer                                     as customer_id,
       ((i * 7 % 100000) / 100.0)::decimal(12,2)             as amount,
       case when i % 3 = 0 then 'paid'
            when i % 3 = 1 then 'open'
            else 'void' end                                  as status,
       (i % 11 = 0)                                          as is_refunded,
       (timestamp '2024-01-01 00:00:00'
            + (i % 86400) * interval '1 second')             as created_at,
       case when i % 13 = 0 then null
            else 'note ' || i::varchar end                   as note
from generate_series(1, {n}) as s(i)
"""

#: Each PostgreSQL table is the DuckDB table plus one planted difference.
#: `expect` is (key, kind, columns-that-must-be-named).
PLANTS: dict[str, tuple[str, tuple[int, str, list[str]] | None]] = {
    "clean": ("", None),
    "changed": (
        "update {t} set amount = amount + 0.01 where id = 12345",
        (12345, "different", ["amount"]),
    ),
    "deleted": (
        "delete from {t} where id = 777",
        (777, "only_in_b", []),
    ),
    "extra": (
        (
            "insert into {t} values (999999999, 1, 1.00, 'paid', false, "
            "timestamp '2024-01-01 00:00:00', 'extra')"
        ),
        (999999999, "only_in_a", []),
    ),
    "null_trap": (
        # NULL becomes ''. The bug class naive implementations pass over.
        "update {t} set note = '' where id = 13",
        (13, "different", ["note"]),
    ),
    "bool_trap": (
        # FALSE becomes NULL. Invisible to any tool whose boolean encoding
        # sends NULL down a CASE `else` branch.
        "update {t} set is_refunded = null where id = 110",
        (110, "different", ["is_refunded"]),
    ),
    "timestamp_shift": (
        (
            "update {t} set created_at = created_at + interval '1 microsecond' "
            "where id = 4096"
        ),
        (4096, "different", ["created_at"]),
    ),
    "sub_scale": (
        # Below the 6-decimal comparison scale, so it must NOT be reported.
        # decimal(12,2) cannot hold it, so widen the column first.
        (
            "alter table {t} alter column amount type decimal(20,10); "
            "update {t} set amount = amount + 0.0000000001 where id = 500"
        ),
        None,
    ),
}


@pytest.fixture(scope="module")
def duck_path(tmp_path_factory) -> str:
    """Side B's file: one pristine table, written once then never again."""
    path = str(tmp_path_factory.mktemp("integration") / "b.duckdb")
    con = duckdb_write(path)
    try:
        con.execute(f"create table orders as {SELECT.format(n=N)}")
    finally:
        con.close()
    return path


@pytest.fixture(scope="module")
def duck(duck_path):
    """Side B: one pristine table, opened read-only."""
    d = open_duckdb(duck_path, side="B")
    yield d
    d.close()


@pytest.fixture(scope="module")
def pg_tables(pg_url):
    """Side A: one table per planted difference, in a schema the tests own."""
    import psycopg

    con = psycopg.connect(pg_url, autocommit=True)
    try:
        con.execute(f"drop schema if exists {PG_SCHEMA}_it cascade")
        con.execute(f"create schema {PG_SCHEMA}_it")
        for name, (sql, _expect) in PLANTS.items():
            table = f"{PG_SCHEMA}_it.o_{name}"
            con.execute(f"create table {table} as {SELECT.format(n=N)}")
            if sql:
                for statement in sql.format(t=table).split("; "):
                    con.execute(statement)
    finally:
        con.close()
    return {name: f"{PG_SCHEMA}_it.o_{name}" for name in PLANTS}


@pytest.fixture(scope="module")
def pg(pg_url, pg_tables):
    """Side A's dialect, over the tables the fixture above created."""
    d = open_pg(pg_url, side="A")
    yield d
    d.close()


pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# The core claim
# ---------------------------------------------------------------------------


def test_identical_tables_across_engines_download_nothing(pg, pg_tables, duck):
    """The core claim, across two genuinely different engines."""
    result = diff(pg, duck, pg_tables["clean"], "main.orders", "id")

    assert result.identical, [
        (d.key, d.kind, d.columns, d.values_a, d.values_b) for d in result.diffs[:5]
    ]
    assert result.stats.rows_downloaded == 0, (
        "PostgreSQL and DuckDB agreed, so not one row should have moved"
    )
    assert result.stats.queries == 4
    assert result.stats.rows_compared_a == result.stats.rows_compared_b == N
    assert not result.warnings


@pytest.mark.parametrize("name", [n for n, (_, e) in PLANTS.items() if e])
def test_each_planted_difference_is_found_exactly(pg, pg_tables, duck, name):
    """Each planted defect is found, alone, and named by kind and column."""
    key, kind, columns = PLANTS[name][1]
    result = diff(pg, duck, pg_tables[name], "main.orders", "id")

    assert [(d.key, d.kind) for d in result.diffs] == [(key, kind)], (
        f"{name}: expected exactly one {kind} at {key}, got "
        f"{[(d.key, d.kind) for d in result.diffs]}"
    )
    if columns:
        assert result.diffs[0].columns == columns
    assert not result.truncated


def test_a_difference_below_the_float_scale_is_not_reported(
    pg, pg_tables, duck, pg_url, duck_path
):
    """The documented limitation, asserted rather than assumed.

    A 1e-10 change is genuinely invisible at 6 decimal places. Reporting a
    match here is correct - but only defensible because the scale is printed
    on every single run.
    """
    result = diff(pg, duck, pg_tables["sub_scale"], "main.orders", "id")
    assert result.identical, [d.key for d in result.diffs[:5]]

    # ... and the same change is caught once the scale is fine enough, which is
    # what proves the miss above was the scale and not a hole in the walk.
    fine_a = open_pg(pg_url, side="A", float_scale=10)
    fine_b = open_duckdb(duck_path, side="B", float_scale=10)
    try:
        fine = diff(fine_a, fine_b, pg_tables["sub_scale"], "main.orders", "id")
        assert [d.key for d in fine.diffs] == [500], (
            "a 1e-10 change must become visible at --float-scale 10"
        )
    finally:
        fine_a.close()
        fine_b.close()


# ---------------------------------------------------------------------------
# Cross-engine specifics that only real SQL can prove
# ---------------------------------------------------------------------------


def test_the_checksum_does_not_overflow_on_a_full_table(pg, pg_tables, duck):
    """20k rows of 60-bit hashes exceed 2**64 when summed.

    A 64-bit accumulator would wrap - differently on each engine - and the two
    sides would disagree on identical data. This is why both dialects aggregate
    in a wider type.
    """
    cols = [c for c in duck.columns("main.orders") if c.name != "id"]
    a_sums = pg.segment_checksums(
        pg_tables["clean"], pg.key_spec(pg_tables["clean"], "id"), cols, 1, N + 1, 1
    )
    b_sums = duck.segment_checksums(
        "main.orders", duck.key_spec("main.orders", "id"), cols, 1, N + 1, 1
    )

    assert a_sums == b_sums
    total = a_sums[0][1]
    assert total > 2**64, f"checksum {total} is too small to prove the point"
    assert isinstance(total, int)


def test_row_counts_and_checksums_agree_bucket_for_bucket(pg, pg_tables, duck):
    """Not just the total - every bucket, so a compensating error cannot hide."""
    cols = [c for c in duck.columns("main.orders") if c.name != "id"]
    a_sums = pg.segment_checksums(
        pg_tables["clean"], pg.key_spec(pg_tables["clean"], "id"), cols, 1, N + 1, 64
    )
    b_sums = duck.segment_checksums(
        "main.orders", duck.key_spec("main.orders", "id"), cols, 1, N + 1, 64
    )

    assert set(a_sums) == set(b_sums) == set(range(64))
    assert a_sums == b_sums
    assert sum(count for count, _ in a_sums.values()) == N


def test_the_key_range_is_walked_with_no_gaps(pg, pg_tables, duck, pg_url):
    """Every row of a fully-different table must be found, not most of them."""
    import psycopg

    con = psycopg.connect(pg_url, autocommit=True)
    table = f"{PG_SCHEMA}_it.o_allchanged"
    try:
        con.execute(f"drop table if exists {table}")
        con.execute(f"create table {table} as {SELECT.format(n=2000)}")
        con.execute(f"update {table} set status = 'CHANGED'")
    finally:
        con.close()

    # A fresh connection: the module-scoped `pg` dialect holds a REPEATABLE
    # READ snapshot taken before this table existed, so it genuinely cannot
    # see it. That is the snapshot working, not a bug - but it means a
    # long-lived Dialect only ever sees the database as of its first query.
    fresh = open_pg(pg_url, side="A")
    try:
        # max_diffs=None is the explicit opt-out from the default retention
        # bound; this test is about the walk covering everything, so it has to
        # actually keep everything.
        result = diff(
            fresh, duck, table, "main.orders", "id", threshold=100, max_diffs=None
        )
    finally:
        fresh.close()
    # Side B has N rows, side A only 2000: 2000 changed plus the rest missing.
    changed = [d for d in result.diffs if d.kind == "different"]
    missing = [d for d in result.diffs if d.kind == "only_in_b"]
    assert len(changed) == 2000, f"found {len(changed)} of 2000 changed rows"
    assert len(missing) == N - 2000
    assert {d.key for d in changed} == set(range(1, 2001))


def test_engines_can_be_swapped_without_changing_the_verdict(pg, pg_tables, duck):
    """A diff is symmetric: swapping the sides only swaps only_in_a/only_in_b."""
    forward = diff(pg, duck, pg_tables["extra"], "main.orders", "id")
    backward = diff(duck, pg, "main.orders", pg_tables["extra"], "id")

    assert [(d.key, d.kind) for d in forward.diffs] == [(999999999, "only_in_a")]
    assert [(d.key, d.kind) for d in backward.diffs] == [(999999999, "only_in_b")]


# ---------------------------------------------------------------------------
# Through the CLI, the way a user actually runs it
# ---------------------------------------------------------------------------


def _cli(pg_url: str, table: str, duck_path: str, *extra: str) -> tuple[int, str]:
    """Drive the real CLI across the two engines. Returns (code, output)."""
    import io

    out = io.StringIO()
    code = main(
        [
            "diff",
            "--a",
            pg_url,
            "--a-table",
            table,
            "--b",
            f"duckdb:///{duck_path}",
            "--b-table",
            "main.orders",
            "--key",
            "id",
            *extra,
        ],
        out=out,
        err=out,
    )
    return code, out.getvalue()


def test_cli_exit_codes_across_engines(pg_url, pg_tables, duck_path):
    """0, 1 and 2 all reachable through the real command line, cross-engine."""
    code, out = _cli(pg_url, pg_tables["clean"], duck_path)
    assert code == EXIT_IDENTICAL, out

    code, out = _cli(pg_url, pg_tables["changed"], duck_path)
    assert code == EXIT_DIFFERENCES, out

    code, out = _cli(pg_url, "public.no_such_table", duck_path)
    assert code == EXIT_ERROR
    assert "side A" in out


def test_cli_json_across_engines(pg_url, pg_tables, duck_path):
    """--json carries the NULL-versus-empty-string trap as raw canonical text."""
    code, out = _cli(pg_url, pg_tables["null_trap"], duck_path, "--json")
    payload = json.loads(out)

    assert code == EXIT_DIFFERENCES
    assert payload["identical"] is False
    assert payload["difference_count"] == 1
    (d,) = payload["differences"]
    assert d["key"] == 13 and d["columns"] == ["note"]
    # PostgreSQL has '' and DuckDB still has NULL.
    assert d["a"] == {"note": ""}
    assert d["b"] == {"note": "\\N"}
    assert payload["stats"]["rows_a"] == N


# ---------------------------------------------------------------------------
# Snapshot consistency
# ---------------------------------------------------------------------------


def test_the_walk_sees_one_consistent_snapshot(pg_url, duck):
    """A live source table must not change underneath the bisection.

    Under PostgreSQL's default READ COMMITTED every statement gets a fresh
    snapshot, so the checksums at one bisection level describe a different
    table than the level below. The tool could then report a difference that
    never existed at any single moment. The source side of a migration is live
    by definition, so this is the normal case, not an edge case.
    """
    import psycopg

    writer = psycopg.connect(pg_url, autocommit=True)
    table = f"{PG_SCHEMA}_it.o_snapshot"
    try:
        writer.execute(f"drop table if exists {table}")
        writer.execute(f"create table {table} as {SELECT.format(n=1000)}")

        reader = open_pg(pg_url, side="A")
        try:
            cols = [c for c in reader.columns(table) if c.name != "id"]
            before = reader.key_stats(table, reader.key_spec(table, "id"))

            # Someone writes to the table mid-walk.
            writer.execute(
                f"insert into {table} values (999999, 1, 1.00, 'paid', false, "
                f"timestamp '2024-01-01 00:00:00', 'inserted mid-walk')"
            )
            writer.execute(f"update {table} set status = 'CHANGED' where id = 500")

            after = reader.key_stats(table, reader.key_spec(table, "id"))
            sums = reader.segment_checksums(
                table, reader.key_spec(table, "id"), cols, 1, 1_000_001, 1
            )

            assert after.rows == before.rows == 1000, (
                "the walk saw the row count change underneath it"
            )
            assert sums[0][0] == 1000, "a later query saw the mid-walk insert"
        finally:
            reader.close()
    finally:
        writer.execute(f"drop table if exists {table}")
        writer.close()


def test_the_connection_refuses_writes(pg_url):
    """Read-only by construction, enforced by the server."""
    import psycopg

    d = open_pg(pg_url, side="A")
    try:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            d.query("create table parity_should_not_exist (x integer)")
    finally:
        d.close()


# ---------------------------------------------------------------------------
# Timezone-aware timestamps
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tz_tables(pg_url, tmp_path_factory):
    """The same two instants on both sides, plus one genuinely shifted row."""
    import psycopg

    path = str(tmp_path_factory.mktemp("tz") / "tz.duckdb")
    con = duckdb_write(path)
    con.execute("create table tz (id bigint, ts timestamptz)")
    con.execute(
        "insert into tz values "
        "(1, timestamptz '2024-06-15 12:00:00+00'), "
        "(2, timestamptz '2024-01-15 12:00:00+00'), "
        "(3, timestamptz '2024-03-01 09:00:00+00')"
    )
    con.close()

    table = f"{PG_SCHEMA}_it.tz"
    writer = psycopg.connect(pg_url, autocommit=True)
    try:
        writer.execute(f"drop table if exists {table}")
        writer.execute(f"create table {table} (id bigint, ts timestamptz)")
        writer.execute(
            f"insert into {table} values "
            f"(1, timestamptz '2024-06-15 12:00:00+00'), "
            # Same instant, written with a different offset. Must still match.
            f"(2, timestamptz '2024-01-15 13:00:00+01'), "
            # A genuinely different instant: the negative control.
            f"(3, timestamptz '2024-03-01 10:00:00+00')"
        )
    finally:
        writer.close()
    return table, path


def test_the_same_instant_matches_whatever_the_server_timezones_are(pg_url, tz_tables):
    """`timestamptz` renders through the *session* timezone.

    Two sides whose sessions differ turn one instant into different text, so
    every row holding a timestamptz reports as changed - a false positive
    indistinguishable from catastrophic data loss. Both dialects therefore pin
    their session to UTC. Verified here against a database whose own default is
    deliberately set to the other side of the world.
    """
    import psycopg

    table, duck_path = tz_tables
    admin = psycopg.connect(pg_url, autocommit=True)
    dbname = admin.info.dbname
    try:
        admin.execute(f"alter database \"{dbname}\" set timezone to 'Asia/Tokyo'")

        a = open_pg(pg_url, side="A")
        b = open_duckdb(duck_path, side="B")
        try:
            assert a.query("show timezone")[0][0] == "UTC"
            assert b.query("select current_setting('TimeZone')")[0][0] == "UTC"

            result = diff(a, b, table, "main.tz", "id")
            # Rows 1 and 2 are the same instants written with different
            # offsets, so only row 3 - a genuinely different instant - counts.
            assert [(d.key, d.kind) for d in result.diffs] == [(3, "different")], (
                f"expected only the genuinely shifted row, got "
                f"{[(d.key, d.values_a, d.values_b) for d in result.diffs]}"
            )
            assert result.diffs[0].columns == ["ts"]
        finally:
            a.close()
            b.close()
    finally:
        admin.execute(f'alter database "{dbname}" reset timezone')
        admin.close()


def test_a_naive_timestamp_is_unaffected_by_the_utc_pin(pg, pg_tables, duck):
    """Pinning UTC must not disturb `timestamp without time zone`, which
    carries no zone at all - the clean table is full of them."""
    result = diff(pg, duck, pg_tables["clean"], "main.orders", "id")
    assert result.identical


# ---------------------------------------------------------------------------
# Restricted accounts - what you actually point at a production warehouse
# ---------------------------------------------------------------------------


def test_a_table_that_exists_is_distinguished_from_one_that_does_not(pg, pg_tables):
    """The catalog probe behind the permissions message.

    `information_schema` is filtered to what the role holds privileges on, so a
    missing GRANT arrives as the same empty result as a missing table. Telling
    someone their table does not exist when it plainly does sends them hunting
    for a typo instead of asking their DBA - and a restricted account is exactly
    what anyone sane points at a production warehouse.

    `pg_catalog` is world-readable, which is what makes the two cases separable.
    """
    schema, name = pg.split_table(pg_tables["clean"], "public")
    assert pg._exists_but_unreadable(schema, name) is True
    assert pg._exists_but_unreadable(schema, "definitely_not_here") is False
    assert pg._exists_but_unreadable("no_such_schema", name) is False


def test_the_permissions_message_replaces_the_not_found_one(pg, monkeypatch):
    """Given the probe says the table is there, the wording has to change -
    otherwise the diagnosis is computed and then thrown away."""
    monkeypatch.setattr(type(pg), "_exists_but_unreadable", lambda *_: True)
    with pytest.raises(ValueError) as exc:
        pg.columns(f"{PG_SCHEMA}_it.pretend_unreadable")
    message = str(exc.value)
    assert "side A" in message
    assert "cannot read it" in message and "SELECT" in message
    assert "not found" not in message, (
        f"a permissions problem must not be reported as absence: {message}"
    )


def test_a_genuinely_absent_table_still_says_not_found(pg):
    """Negative control: the permissions message must not swallow the ordinary
    case, or every typo becomes a wild goose chase after a GRANT."""
    with pytest.raises(ValueError) as exc:
        pg.columns(f"{PG_SCHEMA}_it.definitely_not_here")
    message = str(exc.value)
    assert "not found" in message
    assert "cannot read it" not in message


def test_a_connection_failure_names_the_side(duck_path):
    """A bare driver error says nothing about which of the two endpoints
    failed, which is the first thing anyone needs to know."""
    from parity.dialects.base import get_dialect

    with pytest.raises(ValueError) as exc:
        get_dialect("postgres://nobody:nobody@127.0.0.1:59999/nope", side="B")
    message = str(exc.value)
    assert "side B" in message and "could not connect" in message
