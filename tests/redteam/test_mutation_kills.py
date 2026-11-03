"""Tests that kill mutations the rest of the suite let survive.

Each test here was written against a specific planted bug from
`tests/redteam/mutate.py` (its id is in the docstring): the test fails with the
bug in place and passes without it. They pin *behaviour* - what the engine
actually returns - rather than the SQL text, wherever a live engine or an
in-process stand-in makes that possible, because a string assertion only
proves the code still says what it said yesterday.

Live tests create objects prefixed `rt_mut_` and drop them afterwards.
"""

from __future__ import annotations

import struct
import sys
import types
from typing import Any

import pytest
from conftest import MYSQL_URL, open_mysql, open_pg

from parity.dialects.base import NULL_SENTINEL, canonical_float_text
from parity.types import Column, LogicalType

# ------------------------------------------------------------------- engine

COLS = [
    Column("amount", LogicalType.DECIMAL, "decimal(12,2)"),
    Column("status", LogicalType.STRING, "varchar"),
]


def _run(table_a: Any, table_b: Any, **kwargs: Any) -> Any:
    """Diff two fixture tables through the in-memory fake dialect."""
    from fakes import FakeDialect

    from parity.engine import diff

    return diff(
        FakeDialect(table_a, "A"), FakeDialect(table_b, "B"), "a", "b", "id", **kwargs
    )


def test_one_difference_trimmed_from_a_single_bucket_still_marks_truncated():
    """E12, E13: the smallest possible partial answer is still partial.

    Three differences sit in one downloaded bucket and `max_diffs` is 2, so
    the walk ends with an empty queue and exactly one difference trimmed off.
    The existing truncation tests all trim many, so `if unchecked > 1` and
    dropping the trim from `unchecked` both survived - and both report a
    two-row list as if it were the whole answer.
    """
    from fakes import SyntheticTable

    changed = dict.fromkeys((1, 2, 3), ("0.00", "wrong"))
    result = _run(SyntheticTable(100), SyntheticTable(100, changed=changed), max_diffs=2)
    assert [d.key for d in result.diffs] == [1, 2]
    assert result.truncated, "a trimmed difference went unreported"
    assert not result.identical
    assert any("1 further difference" in w for w in result.warnings), result.warnings


def test_the_walk_stops_as_soon_as_the_limit_is_reached():
    """E14: `len(diffs) >= max_diffs`, not `>`.

    Not a false match - the trim and the truncated flag still fire - but the
    limit exists to bound work and memory on a mismatched pair, and an
    off-by-one there downloads one more bucket per level than it promised.
    Pinned by the rows downloaded: only the first differing bucket (keys 1-4,
    both sides) may be fetched.
    """
    from fakes import SyntheticTable

    changed = {1: ("0.00", "wrong"), 90: ("0.00", "wrong")}
    result = _run(SyntheticTable(100), SyntheticTable(100, changed=changed), max_diffs=1)
    assert [d.key for d in result.diffs] == [1]
    assert result.truncated
    assert result.stats.rows_downloaded == 8


def test_a_bucket_of_exactly_threshold_rows_is_downloaded_not_split():
    """E4: `--threshold` is inclusive (`<=`).

    Not a false match either way, but the documented contract is "download a
    differing bucket once it holds at most this many rows", and the query
    count is pinned everywhere else as a hardware-independent property. Bucket
    0 of 100 keys at factor 32 holds keys 1-4, so at threshold 4 it is fetched
    straight away: 2 key_stats + 2 checksum + 2 fetch queries, 8 rows.
    """
    from fakes import SyntheticTable

    result = _run(
        SyntheticTable(100),
        SyntheticTable(100, changed={2: ("0.00", "wrong")}),
        threshold=4,
    )
    assert [(d.key, d.kind) for d in result.diffs] == [(2, "different")]
    assert (result.stats.queries, result.stats.rows_downloaded) == (6, 8)


# --------------------------------------------------------------- PostgreSQL


@pytest.mark.postgres
def test_pg_float_text_is_pinned_even_when_the_session_default_drops_digits(pg_url):
    """P5: `set extra_float_digits = 1` at connect is load-bearing.

    The suite only ever connects with the server default (1), so removing the
    pin changed nothing it could see. A role or database configured with
    `extra_float_digits = 0` - a real, if old-fashioned, setting - makes
    PostgreSQL print 15 significant digits, so 1e15 + 0.5 renders as `1e+15`
    and canonicalises to `...0.000000` while every other engine says
    `...0.500000`. Simulated here with a libpq `options` startup parameter.
    """
    sep = "&" if "?" in pg_url else "?"
    a = open_pg(pg_url + sep + "options=-c%20extra_float_digits%3D0")
    try:
        col = Column("x", LogicalType.FLOAT, "double precision")
        for x in (1e15 + 0.5, 0.1 + 0.2, 123456.7890123):
            got = a.query(
                f"select {a.normalize(col)} from (select {x!r}::float8 as x) t"
            )[0][0]
            assert got == canonical_float_text(x), x
    finally:
        a.close()


# -------------------------------------------------------------------- MySQL


def _mysql_writer() -> Any:
    """A plain autocommit connection for building and mutating fixtures."""
    from urllib.parse import unquote, urlparse

    import mysql.connector

    u = urlparse(MYSQL_URL)
    return mysql.connector.connect(
        host=u.hostname or "127.0.0.1",
        port=u.port or 3306,
        user=unquote(u.username) if u.username else None,
        password=unquote(u.password) if u.password else None,
        database=u.path.lstrip("/") or None,
        autocommit=True,
    )


@pytest.mark.mysql
def test_mysql_walk_reads_one_snapshot_of_a_live_table(mysql_url):
    """M11: the MySQL snapshot depends on autocommit being *off*.

    `test_the_session_is_utc_and_snapshot_isolated` checks the isolation
    variable, which still reads REPEATABLE-READ under autocommit - but then
    every statement is its own transaction with its own snapshot, so a row
    written mid-walk is seen by one bisection level and not the one before.
    """
    writer = _mysql_writer()
    cur = writer.cursor()
    table = "rt_mut_snapshot"
    try:
        cur.execute(f"drop table if exists {table}")
        cur.execute(f"create table {table} (id bigint primary key, v varchar(10))")
        cur.execute(
            f"insert into {table} values "
            + ", ".join(f"({i}, 'v')" for i in range(1, 101))
        )
        reader = open_mysql(mysql_url, side="A")
        try:
            key = reader.key_spec(table, "id")
            before = reader.key_stats(table, key)
            cur.execute(f"insert into {table} values (1000, 'mid-walk')")
            cur.execute(f"update {table} set v = 'CHANGED' where id = 50")
            after = reader.key_stats(table, key)
            assert before.rows == after.rows == 100, "the walk saw a mid-walk insert"
        finally:
            reader.close()
    finally:
        cur.execute(f"drop table if exists {table}")
        cur.close()
        writer.close()


# ------------------------------------------------------- Snowflake (offline)


class _FakeSnowflakeCursor:
    """Records statements; Snowflake's connect only issues the UTC pin."""

    def __init__(self, log: list[str]) -> None:
        """Share the connection's statement log."""
        self.log = log

    def execute(self, sql: str) -> None:
        """Record one statement."""
        self.log.append(sql)

    def close(self) -> None:
        """Nothing to release."""


def test_snowflake_connect_pins_the_session_to_utc(monkeypatch):
    """F5: `connect` is `# pragma: no cover` and only runs against a real
    account, so dropping its UTC pin was invisible offline. Without it, two
    accounts in different regions render one TIMESTAMP_LTZ instant as
    different text and every row reports as changed.
    """
    log: list[str] = []
    connector = types.ModuleType("snowflake.connector")

    class Conn:
        """Stands in for a snowflake.connector connection."""

        def cursor(self) -> _FakeSnowflakeCursor:
            """A cursor sharing the statement log."""
            return _FakeSnowflakeCursor(log)

    def connect(**kwargs: Any) -> Conn:
        """Accept the dialect's keyword arguments and hand back a fake."""
        return Conn()

    connector.connect = connect  # type: ignore[attr-defined]
    package = types.ModuleType("snowflake")
    package.connector = connector  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "snowflake", package)
    monkeypatch.setitem(sys.modules, "snowflake.connector", connector)

    from parity.dialects.snowflake_dialect import SnowflakeDialect

    SnowflakeDialect(side="A").connect("snowflake://u:p@acct/db/sch?warehouse=w")
    assert any("timezone" in s.lower() and "'utc'" in s.lower() for s in log), (
        f"no UTC pin among {log}"
    )


def test_a_row_count_difference_alone_marks_a_bucket_as_differing():
    """E6: a bucket differs when its row count differs, even if the checksums agree.

    The checksum is a sum of 60-bit hashes, so a sum can agree while the
    multisets behind it do not (a hash collision, or an extra row whose
    contribution happens to cancel). The count is the cheap second witness,
    and every existing test plants differences that move *both* numbers, so
    comparing the checksum alone survived. Simulated by giving side B side A's
    checksums but its own counts: B holds one row more, and the engine must
    still go and find it.
    """
    from fakes import FakeDialect, SyntheticTable

    from parity.engine import diff

    a = FakeDialect(SyntheticTable(100), "A")
    b = FakeDialect(SyntheticTable(101), "B")
    real = b.segment_checksums

    def collided(*args: Any, **kwargs: Any) -> dict[int, tuple[int, int]]:
        """B's counts, A's checksums: the same sums over different rows."""
        mine = real(*args, **kwargs)
        theirs = FakeDialect.segment_checksums(a, *args, **kwargs)
        return {i: (cnt, theirs.get(i, (0, 0))[1]) for i, (cnt, _) in mine.items()}

    b.segment_checksums = collided  # type: ignore[method-assign]
    result = diff(a, b, "a", "b", "id")
    assert [(d.key, d.kind) for d in result.diffs] == [(101, "only_in_b")]
    assert not result.identical


def test_threshold_bounds_the_larger_side_not_the_smaller():
    """E8: a bucket is downloaded once *both* sides hold at most `threshold` rows.

    Not a false match, but `--threshold` is what bounds a single fetch, and
    the side that matters is the big one: `min(...)` lets a bucket that is
    empty on one side (a deleted range, or simply the wrong table on side B)
    be fetched whole from the other side, however large. Here B has lost the
    first 1,000 keys; with threshold 10 and max_diffs 5 the walk must narrow
    to a bucket of at most 10 rows and stop, never pull a 63-row bucket.
    """
    from fakes import SyntheticTable

    result = _run(
        SyntheticTable(2000),
        SyntheticTable(2000, deleted=range(1, 1001)),
        threshold=10,
        max_diffs=5,
    )
    assert result.truncated
    assert all(d.kind == "only_in_a" for d in result.diffs)
    assert result.stats.rows_downloaded <= 10, result.stats.rows_downloaded


def test_a_walk_cut_short_before_any_difference_is_not_identical():
    """T1: `identical` must be false when `truncated` is set, even with no diffs.

    Every truncation test so far stops *after* collecting differences, so
    `diffs` is never empty and `identical` was false for that reason alone.
    `max_diffs=0` (a library caller asking for "just tell me if anything is
    off") stops before downloading a single row: no diffs, truncated - and
    without the `truncated` guard the result reads as a proven match of two
    tables that differ. A false match.
    """
    from fakes import SyntheticTable

    from parity.types import DiffResult, DiffStats

    assert not DiffResult(
        diffs=[], stats=DiffStats(), columns=[], truncated=True
    ).identical
    result = _run(
        SyntheticTable(100), SyntheticTable(100, changed={7: ("0.00", "x")}), max_diffs=0
    )
    assert result.diffs == []
    assert result.truncated
    assert not result.identical


def test_values_swapped_between_two_keys_are_found(tmp_path):
    """B8: the key must be folded into the row hash a real dialect computes.

    Two rows in one bucket trade their values: A holds (1, 'v1') and
    (2, 'v2'), B holds (1, 'v2') and (2, 'v1'). Hash only the non-key columns and both
    buckets hold the same two hashes - same count, same sum - so the walk
    calls them identical: a false match, and a real migration bug class (a
    join that mis-assigned rows to keys). The fake dialect folds the key on
    its own, so only a real engine can see this mutation.
    """
    from conftest import duckdb_write, open_duckdb

    from parity.engine import diff

    path = str(tmp_path / "rt_mut_swap.duckdb")
    con = duckdb_write(path)
    con.execute("create table a (id bigint, v varchar)")
    con.execute("create table b (id bigint, v varchar)")
    con.execute("insert into a select i, 'v' || i from range(1, 1001) t(i)")
    con.execute("insert into b select i, 'v' || i from range(1, 1001) t(i)")
    con.execute(
        "update b set v = case id when 1 then 'v2' else 'v1' end where id in (1, 2)"
    )
    con.close()
    side_a, side_b = open_duckdb(path, "A"), open_duckdb(path, "B")
    try:
        # A small threshold, so the walk has to trust a bucket checksum
        # rather than download the whole table and compare rows directly.
        result = diff(side_a, side_b, "a", "b", "id", threshold=4)
    finally:
        side_a.close()
        side_b.close()
    assert [(d.key, d.kind) for d in result.diffs] == [(1, "different"), (2, "different")]
    assert not result.identical


@pytest.mark.postgres
def test_pg_real_column_renders_through_its_double_value(pg_url):
    """P13: a single-precision column is widened to float8 before rendering.

    Every live FLOAT fixture is `double precision`, where the widening cast is
    a no-op. On a `real` column PostgreSQL's own float4 shortest repr prints
    -4088288.25 as `-4088288.2`, so it canonicalises to `-4088288.200000`
    while DuckDB and MySQL (which widen) say `-4088288.250000` - every
    migrated `real` row would report as changed.
    """
    a = open_pg(pg_url)
    try:
        col = Column("x", LogicalType.FLOAT, "real")
        for x in (-4088288.25, 0.1, 16777215.0, 3.4028234663852886e38 / 1e30):
            got = a.query(
                f"select {a.normalize(col)} from (select ({x!r})::real as x) t"
            )[0][0]
            widened = struct.unpack("f", struct.pack("f", x))[0]
            assert got == canonical_float_text(widened), (x, got)
    finally:
        a.close()


@pytest.mark.mysql
def test_mysql_null_boolean_renders_the_sentinel_not_false(mysql_url):
    """M1: MySQL's BOOLEAN must use `when not c`, never `else` (CLAUDE.md 4.3).

    The PostgreSQL and DuckDB versions of this bug are caught by planted
    NULL-versus-FALSE differences; no MySQL fixture holds a NULL boolean, so
    `else 'false'` survived there. With it, a NULL TINYINT(1) renders
    'false' - identical to a real FALSE on the other side: a false match.
    """
    a = open_mysql(mysql_url)
    try:
        col = Column("b", LogicalType.BOOLEAN, "tinyint(1)")
        rows = a.query(
            f"select {a.normalize(col)} from (select cast(null as signed) as b "
            f"union all select 0 union all select 1) t"
        )
        assert sorted(r[0] for r in rows) == sorted([NULL_SENTINEL, "false", "true"]), (
            rows
        )
    finally:
        a.close()


@pytest.mark.mysql
def test_mysql_timestamp_keeps_its_microseconds(mysql_url):
    """M9: MySQL renders DATETIME(6) to the microsecond, like every engine.

    No MySQL fixture holds a fractional second, so dropping `%f` survived -
    and with it two timestamps that differ only below the second (a
    truncating ETL step, a precision-losing column type) compare equal: a
    false match.
    """
    a = open_mysql(mysql_url)
    try:
        col = Column("t", LogicalType.TIMESTAMP, "datetime")
        rows = a.query(
            f"select {a.normalize(col)} from (select cast("
            f"'2024-02-29 13:04:05.123456' as datetime(6)) as t) x"
        )
        assert rows[0][0] == "2024-02-29 13:04:05.123456", rows
    finally:
        a.close()
