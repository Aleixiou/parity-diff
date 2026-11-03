"""Snowflake against DuckDB, end to end - the verification the draft needs.

DRAFT dialect (see src/parity/dialects/snowflake_dialect.py): Snowflake earns
the word "supported" only once this file passes against a live account, byte
for byte with DuckDB. It is the Snowflake twin of test_mysql.py, and the only
Snowflake-specific content is the fixture DDL and the three decisions the draft
made: the row hash goes through FLOOR(MD5_NUMBER_UPPER64(x)/16), integer and
decimal are split by numeric_scale, and the NULL sentinel is CHR(92)||'N'. Each
is verified below by the values agreeing with DuckDB, not by reading the SQL.

Skips cleanly unless PARITY_TEST_SNOWFLAKE points at a reachable account, so it
never runs in CI and never spends warehouse credits on its own. Run it with:

    pip install -e ".[duckdb,snowflake]" pytest
    set PARITY_TEST_SNOWFLAKE=snowflake://user:pw@account/PARITY_TEST/ENC?warehouse=PARITY_WH&role=PARITY_RO
    pytest tests/test_snowflake.py -v
"""

from __future__ import annotations

import pytest
from conftest import duckdb_write, open_duckdb, open_snowflake

from parity.engine import diff

pytestmark = pytest.mark.snowflake

N = 5_000

#: Side B: the DuckDB reference. Types line up with the Snowflake table below -
#: a real boolean on both sides (Snowflake has one, unlike MySQL), a decimal,
#: a naive timestamp - so any difference the tool reports is a real one, not a
#: type mismatch.
DUCKDB_TABLE = f"""
create table orders as
select i::bigint                                             as id,
       (i % 97)::integer                                     as customer_id,
       ((i * 7 % 100000) / 100.0)::decimal(12,2)             as amount,
       case when i % 3 = 0 then 'paid'
            when i % 3 = 1 then 'open' else 'void' end       as status,
       (i % 11 = 0)                                          as is_refunded,
       (timestamp '2024-01-01 00:00:00'
            + (i % 86400) * interval '1 second')             as created_at,
       case when i % 13 = 0 then null
            else 'note ' || i::varchar end                   as note
from generate_series(1, {N}) as s(i)
"""

#: Side A: the Snowflake table. NUMBER(38,0) reports as an integer only because
#: its scale is 0 - the draft's columns() reads numeric_scale to tell it from
#: the decimal `amount`, and a bug there would render the key as `1.000000`.
SNOWFLAKE_TABLE = """
create or replace table orders (
    id          number(38,0),
    customer_id number(38,0),
    amount      number(12,2),
    status      varchar,
    is_refunded boolean,
    created_at  timestamp_ntz,
    note        varchar
)
"""

#: GENERATOR makes N rows; row_number() turns them into a stable 1..N key. id
#: is materialised in the subquery so each derived column reads one fixed value
#: rather than calling the sequence generator again mid-row.
SNOWFLAKE_FILL = f"""
insert into orders
select id,
       mod(id, 97),
       (mod(id * 7, 100000) / 100.0),
       case when mod(id, 3) = 0 then 'paid'
            when mod(id, 3) = 1 then 'open' else 'void' end,
       (mod(id, 11) = 0),
       dateadd(second, mod(id, 86400), '2024-01-01 00:00:00'::timestamp_ntz),
       case when mod(id, 13) = 0 then null else 'note ' || id::varchar end
from (
    select row_number() over (order by seq4()) as id
    from table(generator(rowcount => {N}))
)
"""


@pytest.fixture(scope="module")
def duck_path(tmp_path_factory) -> str:
    """Side B: the DuckDB reference table, built once and opened read-only."""
    path = str(tmp_path_factory.mktemp("snowflake_it") / "b.duckdb")
    con = duckdb_write(path)
    try:
        con.execute(DUCKDB_TABLE)
    finally:
        con.close()
    return path


def _build_snowflake(snowflake_url: str, plant: str | None) -> None:
    """Create the Snowflake `orders` table, optionally with one planted defect.

    Uses its own connection and commits (autocommit is on), because the dialect
    opened later must see the data. This is the *only* place the tests write to
    Snowflake; the parity dialect itself issues SELECT only.
    """
    a = open_snowflake(snowflake_url, side="A")
    cur = a._conn.cursor()
    try:
        cur.execute(SNOWFLAKE_TABLE)
        cur.execute("truncate table orders")
        cur.execute(SNOWFLAKE_FILL)
        if plant == "changed":
            cur.execute("update orders set amount = amount + 0.01 where id = 1234")
        elif plant == "deleted":
            cur.execute("delete from orders where id = 777")
        elif plant == "null_trap":
            # id 13 is a multiple of 13, so `note` is NULL on the DuckDB side;
            # setting it to '' here plants the NULL-versus-empty-string trap.
            cur.execute("update orders set note = '' where id = 13")
    finally:
        cur.close()
        a.close()


def _diff(snowflake_url: str, duck_path: str, **kwargs):
    """Diff the Snowflake orders table against the DuckDB one."""
    a = open_snowflake(snowflake_url, side="A")
    b = open_duckdb(duck_path, side="B")
    try:
        # Side A names the table as Snowflake stored it (unquoted -> ORDERS);
        # the key is given lower-case on purpose, to exercise the engine's
        # case-insensitive key/column matching against Snowflake's ID/AMOUNT.
        return diff(a, b, "ORDERS", "main.orders", "id", **kwargs)
    finally:
        a.close()
        b.close()


def test_the_hash_constant_agrees_with_the_other_engines(snowflake_url):
    """The whole cross-engine contract in one number.

    Snowflake reaches it through FLOOR(MD5_NUMBER_UPPER64(x)/16), a fourth
    distinct path after PostgreSQL's bit-cast, DuckDB's hex-cast and MySQL's
    CONV. If this disagrees, nothing else can be trusted.
    """
    a = open_snowflake(snowflake_url, side="A")
    try:
        got = a.query(f"select {a.hash_expr(chr(39) + 'abc' + chr(39))}")[0][0]
        assert int(got) == 648541476951500027
    finally:
        a.close()


def test_identical_tables_match_and_download_nothing(snowflake_url, duck_path):
    """The headline claim, cross-engine: agreement moves zero rows."""
    _build_snowflake(snowflake_url, plant=None)
    result = _diff(snowflake_url, duck_path)
    assert result.identical
    assert result.diffs == []
    assert result.stats.rows_downloaded == 0


def test_a_changed_decimal_is_found_on_exactly_that_row(snowflake_url, duck_path):
    """A one-cent change on one row is reported as that row, that column."""
    _build_snowflake(snowflake_url, plant="changed")
    result = _diff(snowflake_url, duck_path)
    assert [(d.key, d.kind) for d in result.diffs] == [(1234, "different")]
    # Columns are reported as side A (Snowflake) stores them - upper-cased.
    assert result.diffs[0].columns == ["AMOUNT"]


def test_a_deleted_row_is_reported_only_in_b(snowflake_url, duck_path):
    """A row missing from Snowflake is only_in_b, not an error."""
    _build_snowflake(snowflake_url, plant="deleted")
    result = _diff(snowflake_url, duck_path)
    assert [(d.key, d.kind) for d in result.diffs] == [(777, "only_in_b")]


def test_null_versus_empty_string_is_caught(snowflake_url, duck_path):
    """The trap naive tools miss: NULL on one side, '' on the other."""
    _build_snowflake(snowflake_url, plant="null_trap")
    result = _diff(snowflake_url, duck_path)
    assert [(d.key, d.kind) for d in result.diffs] == [(13, "different")]
    assert result.diffs[0].columns == ["NOTE"]


# ------------------------------------------- the changes made without an account
#
# Each of these was written from Snowflake's documentation while no account was
# available (CLAUDE.md section 4.11) and is verified here against a live one,
# 2026-11-03, by planting the difference it exists to catch.


def _snowflake_exec(snowflake_url: str, *statements: str) -> None:
    """Run fixture DDL/DML on Snowflake through the driver (autocommit)."""
    a = open_snowflake(snowflake_url, side="A")
    cur = a._conn.cursor()
    try:
        for sql in statements:
            cur.execute(sql)
    finally:
        cur.close()
        a.close()


def _duck_table(tmp_path, name: str, *statements: str) -> str:
    """Build a DuckDB file with `statements` (UTC session); return its path."""
    path = str(tmp_path / f"{name}.duckdb")
    con = duckdb_write(path)
    try:
        con.execute("set TimeZone='UTC'")
        for sql in statements:
            con.execute(sql)
    finally:
        con.close()
    return path


def _diff_sf_duck(snowflake_url, path, a_table, b_table, key, url=None):
    """Diff a Snowflake table (side A) against a DuckDB one (side B)."""
    a = open_snowflake(url or snowflake_url, side="A")
    b = open_duckdb(path, side="B")
    try:
        return diff(a, b, a_table, b_table, key)
    finally:
        a.close()
        b.close()


def test_telemetry_is_off_in_the_session(snowflake_url):
    """CLAUDE.md section 6: no telemetry, and no cloud-metadata probes. The
    connector's own switch is off, and platform detection is disabled before
    it loads. (CLIENT_TELEMETRY_ENABLED is a client parameter: the server does
    not list it under SHOW PARAMETERS, so the connector's state is what can
    be checked.)"""
    import os

    a = open_snowflake(snowflake_url, side="A")
    try:
        assert a._conn.telemetry_enabled is False
        assert os.environ.get("SNOWFLAKE_DISABLE_PLATFORM_DETECTION") == "true"
    finally:
        a.close()


def test_a_session_contacts_only_the_account_and_sends_no_telemetry(
    snowflake_url, caplog
):
    """CLAUDE.md section 6, end to end: everything the driver logs about its
    network use during a session names the account's own login, query and
    logout endpoints - no `/telemetry/send`, no cloud metadata service.
    Driver 5.x failed this (it flushes a telemetry batch at logout and probes
    169.254.169.254), which is why the extra pins 4.x."""
    import logging

    with caplog.at_level(logging.DEBUG):
        a = open_snowflake(snowflake_url, side="A")
        try:
            a.query("select 1")
        finally:
            a.close()
    import snowflake.connector

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "/session/v1/login-request" in text  # the log really shows paths
    assert "/telemetry" not in text
    assert "logs to telemetry" not in text  # 4.x's send line
    assert "flushing telemetry batch" not in text  # 5.x's, which names no path
    assert "169.254.169.254" not in text and "metadata.google.internal" not in text
    major = int(snowflake.connector.__version__.split(".")[0])
    assert major < 5, "pyproject pins <5: 5.x sends telemetry no switch stops"


def test_int_div_is_exact_where_number_division_rounds(snowflake_url):
    """NUMBER division rounds its quotient to 6 places, so a floor of `/`
    puts q.9999996 in bucket q + 1. `int_div` must equal Python's `//` on
    exactly those quotients, and on spans as wide as 2^64 * 64."""
    import random

    rnd = random.Random(5)
    pairs = [(10**7 * d - 1, d) for d in (3, 7, 10**9 + 7, 2**40 + 1)]
    pairs += [(q * d + d - 1, d) for q, d in ((0, 64), (2**63, 64), (12345, 10**12))]
    pairs += [(rnd.randrange(2**70), rnd.randrange(1, 2**40)) for _ in range(200)]
    a = open_snowflake(snowflake_url, side="A")
    try:
        sql = " union all ".join(
            f"select {i} as i, {a.int_div(str(n), str(d))} as q"
            for i, (n, d) in enumerate(pairs)
        )
        got = {int(i): int(q) for i, q in a.query(f"select i, q from ({sql}) order by i")}
    finally:
        a.close()
    want = {i: n // d for i, (n, d) in enumerate(pairs)}
    assert got == want
    # The naive form this replaced really does round on this engine: the
    # quotient 9999999.99999999... comes back as 10000000.000000.
    a = open_snowflake(snowflake_url, side="A")
    try:
        n, d = pairs[2]
        assert int(a.query(f"select floor({n} / {d})")[0][0]) == n // d + 1
    finally:
        a.close()


def test_a_sparse_wide_key_range_buckets_alike_on_both_sides(snowflake_url, tmp_path):
    """Keys across the whole bigint range walk the bucket expression at its
    widest; identical rows must match and one planted change be found."""
    keys = [-(2**63), -(2**40), -1, 0, 1, 999_999_999, 2**53 + 1, 2**62, 2**63 - 1]
    keys += [k * 7_919_000_000_007 for k in range(1, 300)]
    vals = ", ".join(f"({k}, 'v{i}')" for i, k in enumerate(keys))
    _snowflake_exec(
        snowflake_url,
        "create or replace table wide (id number(19,0), v varchar)",
        f"insert into wide values {vals}",
        f"update wide set v = 'changed' where id = {keys[150]}",
    )
    path = _duck_table(
        tmp_path,
        "wide",
        "create table wide (id bigint, v varchar)",
        f"insert into wide values {vals}",
    )
    result = _diff_sf_duck(snowflake_url, path, "WIDE", "main.wide", "id")
    assert [(d.key, d.kind) for d in result.diffs] == [(keys[150], "different")]
    assert not result.unexplained


def test_timestamp_tz_compares_instants_not_wall_clocks(snowflake_url, tmp_path):
    """A TIMESTAMP_TZ prints in its stored offset unless moved to UTC: the
    same instant stored at +05:00 and at UTC must match, and the same wall
    clock at two offsets - instants five hours apart - must not."""
    _snowflake_exec(
        snowflake_url,
        "create or replace table tz (id number(38,0), t timestamp_tz, l timestamp_ltz)",
        "insert into tz select 1, '2024-06-15 12:00:00 +05:00'::timestamp_tz,"
        " '2024-06-15 12:00:00 +05:00'::timestamp_ltz"
        " union all select 2, '2024-01-15 23:30:00.123456 -08:00'::timestamp_tz,"
        " '2024-01-15 23:30:00.123456 -08:00'::timestamp_ltz"
        " union all select 3, '2024-03-01 09:00:00 +05:00'::timestamp_tz,"
        " '2024-03-01 09:00:00 +00:00'::timestamp_ltz",
    )
    path = _duck_table(
        tmp_path,
        "tz",
        "create table tz (id bigint, t timestamptz, l timestamptz)",
        "insert into tz values"
        " (1, '2024-06-15 07:00:00+00', '2024-06-15 07:00:00+00'),"
        " (2, '2024-01-16 07:30:00.123456+00', '2024-01-16 07:30:00.123456+00'),"
        " (3, '2024-03-01 09:00:00+00', '2024-03-01 09:00:00+00')",
    )
    result = _diff_sf_duck(snowflake_url, path, "TZ", "main.tz", "id")
    # Row 3's T is 09:00 at +05:00, i.e. 04:00 UTC: a different instant.
    assert [(d.key, d.kind, d.columns) for d in result.diffs] == [(3, "different", ["T"])]
    # Both are instants on both sides, so no naive/aware warning is printed.
    assert not [w for w in result.warnings if "time zone" in w.lower()]


def test_sub_microsecond_timestamps_are_caveated(snowflake_url):
    """FF6 drops nanoseconds, so a TIMESTAMP(9) column must say so."""
    _snowflake_exec(
        snowflake_url,
        "create or replace table ns (id number(38,0), t9 timestamp_ntz(9),"
        " t6 timestamp_ntz(6))",
    )
    a = open_snowflake(snowflake_url, side="A")
    try:
        caveats = a.caveats(a.columns("NS"))
    finally:
        a.close()
    fine = [c for c in caveats if "nanoseconds" in c]
    assert len(fine) == 1 and "'T9'" in fine[0] and "'T6'" not in fine[0]


def test_a_nanosecond_timestamp_key_keeps_its_digits(snowflake_url, tmp_path):
    """As keys, two instants a nanosecond apart are two rows (RT-B6), and a
    key without sub-microsecond digits still matches DuckDB's TIMESTAMP_NS."""
    rows = [
        ("2024-01-01 00:00:00.123456789", "a"),
        ("2024-01-01 00:00:00.123456788", "b"),
        ("2024-01-01 00:00:00.123456000", "c"),
    ]
    vals = ", ".join(f"('{t}', '{v}')" for t, v in rows)
    _snowflake_exec(
        snowflake_url,
        "create or replace table nskey (t timestamp_ntz(9), v varchar)",
        f"insert into nskey values {vals}",
        "update nskey set v = 'changed' where t = '2024-01-01 00:00:00.123456788'",
    )
    path = _duck_table(
        tmp_path,
        "nskey",
        "create table nskey (t timestamp_ns, v varchar)",
        f"insert into nskey values {vals}",
    )
    result = _diff_sf_duck(snowflake_url, path, "NSKEY", "main.nskey", "t")
    assert [d.kind for d in result.diffs] == ["different"]
    assert result.diffs[0].columns == ["V"]


def test_decimal_keys_match_by_value(snowflake_url, tmp_path):
    """A NUMBER(10,2) key `1.50` is the DuckDB DECIMAL(12,4) key `1.5000`,
    and an integral `42.00` is the integer key on the other side's terms."""
    _snowflake_exec(
        snowflake_url,
        "create or replace table deckey (k number(10,2), v varchar)",
        "insert into deckey values (1.50, 'a'), (42.00, 'b'), (-0.25, 'c'), (0, 'd')",
        "update deckey set v = 'changed' where k = -0.25",
    )
    path = _duck_table(
        tmp_path,
        "deckey",
        "create table deckey (k decimal(12,4), v varchar)",
        "insert into deckey values (1.5, 'a'), (42, 'b'), (-0.25, 'c'), (0, 'd')",
    )
    result = _diff_sf_duck(snowflake_url, path, "DECKEY", "main.deckey", "k")
    assert [(d.kind, d.columns) for d in result.diffs] == [("different", ["V"])]


def test_variant_keeps_the_type_a_varchar_cast_would_lose(snowflake_url):
    """The number 1 and the string "1" print alike through a varchar cast;
    TYPEOF + TO_JSON keeps them apart. Snowflake against Snowflake, since no
    other engine has VARIANT."""
    fill = (
        "insert into {t} select 1, parse_json('1') union all"
        ' select 2, parse_json(\'{{"a": [1, "x"], "b": null}}\') union all'
        " select 3, to_variant('2024-01-01'::date) union all"
        " select 4, null"
    )
    _snowflake_exec(
        snowflake_url,
        "create or replace table var_a (id number(38,0), v variant)",
        "create or replace table var_b (id number(38,0), v variant)",
        fill.format(t="var_a"),
        fill.format(t="var_b"),
        "update var_b set v = to_variant('1') where id = 1",
        "update var_b set v = to_variant('2024-01-01') where id = 3",
    )
    a = open_snowflake(snowflake_url, side="A")
    b = open_snowflake(snowflake_url, side="B")
    try:
        result = diff(a, b, "VAR_A", "VAR_B", "id")
    finally:
        a.close()
        b.close()
    assert [(d.key, d.kind) for d in result.diffs] == [(1, "different"), (3, "different")]


def test_a_schema_qualified_table_reads_that_schema(snowflake_url, tmp_path):
    """RT-D2: the rows must come from the table that was introspected. The
    URL's schema is ENC; `PUBLIC.SAME` holds a planted change that `ENC.SAME`
    does not, so a query left to the session schema would read identical."""
    database = snowflake_url.split("@", 1)[1].split("/")[1]
    _snowflake_exec(
        snowflake_url,
        f"create schema if not exists {database}.PUBLIC",
        f"create or replace table {database}.PUBLIC.SAME (id number(38,0), v varchar)",
        f"insert into {database}.PUBLIC.SAME values (1, 'a'), (2, 'planted')",
        "create or replace table SAME (id number(38,0), v varchar)",
        "insert into SAME values (1, 'a'), (2, 'b')",
    )
    path = _duck_table(
        tmp_path,
        "same",
        "create table same (id bigint, v varchar)",
        "insert into same values (1, 'a'), (2, 'b')",
    )
    assert _diff_sf_duck(snowflake_url, path, "SAME", "main.same", "id").identical
    result = _diff_sf_duck(snowflake_url, path, "PUBLIC.SAME", "main.same", "id")
    assert [(d.key, d.kind) for d in result.diffs] == [(2, "different")]
