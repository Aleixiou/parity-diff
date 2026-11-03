"""BigQuery against DuckDB, end to end, on a live project.

The BigQuery twin of test_snowflake.py, and the proof the dialect has to pass
before the word "supported" is earned: the same rows built on both engines must
render to byte-identical canonical text and sum to the same checksum, and every
planted difference must be found exactly - nothing missed, nothing extra.

Skips cleanly unless PARITY_TEST_BIGQUERY points at a reachable project, so it
never runs in CI and never spends anything on its own. Run it with:

    pip install -e ".[duckdb,bigquery]" pytest
    set PARITY_TEST_BIGQUERY=bigquery://<project>/<dataset>
    pytest tests/test_bigquery.py -v

Fixtures are created through a separate read-write client - the dialect itself
holds a read-only token and could not create them if it tried (a test below
checks exactly that). Every table carries a per-run suffix and is dropped at
the end of the module, so concurrent runs cannot collide and nothing is left
behind in the dataset.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import duckdb_write, open_bigquery, open_duckdb

from parity.dialects.bigquery_dialect import parse_bigquery_url
from parity.engine import diff

pytestmark = pytest.mark.bigquery

N = 5_000
RUN = uuid.uuid4().hex[:8]
WIDE_COLS = 130  # past MAX_CONCAT_ARGS (64), so the other side nests

#: Side B, the DuckDB reference. Every awkward value the encoding has to agree
#: on is in here somewhere: NULLs in every type, a NULL boolean, +/-Infinity,
#: NaN, negative zero, doubles where PostgreSQL and DuckDB already round
#: differently, a year-999 date, microsecond timestamps, an instant
#: (timestamptz) next to a naive one, and non-ASCII text.
DUCKDB_ORDERS = f"""
create table orders as
select i::bigint                                              as id,
       'k' || i::varchar                                      as code,
       (i % 97)::bigint                                       as customer_id,
       ((i * 7 % 100000) / 100.0)::decimal(12,2)              as amount,
       case when i % 19 = 0 then null
            when i = 5 then 'inf'::double
            when i = 6 then '-inf'::double
            when i = 7 then 'nan'::double
            when i = 8 then '-0.0'::double
            when i = 9 then 1e20::double / 3
            when i = 10 then 123456789.1234565::double
            when i = 11 then -1000000000000000.5::double
            else i::double / 7 end                            as ratio,
       case when i % 3 = 0 then 'paid'
            when i % 3 = 1 then 'open' else 'void' end        as status,
       case when i % 17 = 0 then null else i % 11 = 0 end     as is_refunded,
       date '0999-12-25' + i::integer                         as day,
       timestamp '2024-01-01 00:00:00'
           + (i % 86400) * interval '1 second'
           + ((i * 7919) % 1000000) * interval '1 microsecond' as created_at,
       timestamptz '2024-06-15 12:00:00+00'
           + (i * 61) * interval '1 second'                   as paid_at,
       case when i % 13 = 0 then null
            else 'note ' || i::varchar || ' ü✓' end           as note
from generate_series(1, {N}) as s(i)
"""

BIGQUERY_ORDERS = f"""
select i                                                      as id,
       concat('k', cast(i as string))                         as code,
       mod(i, 97)                                             as customer_id,
       cast(mod(i * 7, 100000) as numeric) / 100              as amount,
       case when mod(i, 19) = 0 then null
            when i = 5 then cast('inf' as float64)
            when i = 6 then cast('-inf' as float64)
            when i = 7 then cast('nan' as float64)
            when i = 8 then cast('-0.0' as float64)
            when i = 9 then cast(1e20 as float64) / 3
            when i = 10 then cast('123456789.1234565' as float64)
            when i = 11 then cast('-1000000000000000.5' as float64)
            else i / 7 end                                    as ratio,
       case when mod(i, 3) = 0 then 'paid'
            when mod(i, 3) = 1 then 'open' else 'void' end    as status,
       case when mod(i, 17) = 0 then null else mod(i, 11) = 0 end as is_refunded,
       date_add(date '0999-12-25', interval i day)            as day,
       datetime_add(datetime_add(datetime '2024-01-01 00:00:00',
           interval mod(i, 86400) second),
           interval mod(i * 7919, 1000000) microsecond)       as created_at,
       timestamp_add(timestamp '2024-06-15 12:00:00+00',
           interval i * 61 second)                            as paid_at,
       case when mod(i, 13) = 0 then null
            else concat('note ', cast(i as string), ' ü✓') end as note
from unnest(generate_array(1, {N})) as i
"""


def _wide_select(rows: int, bigquery: bool) -> str:
    """A WIDE_COLS-column table, identical in both engines' SQL."""
    cols = []
    for k in range(WIDE_COLS):
        if k % 3 == 0:
            cols.append(f"i + {k} as c{k}")
        elif k % 3 == 1:
            s = (
                f"concat('v', cast(i * {k} as string))"
                if bigquery
                else f"'v' || (i * {k})"
            )
            cols.append(f"case when mod(i, {k + 2}) = 0 then null else {s} end as c{k}")
        else:
            cols.append(
                f"(i * {k}) / 8 as c{k}" if bigquery else f"(i * {k})::double / 8 as c{k}"
            )
    body = ", ".join(cols)
    if bigquery:
        return f"select i as id, {body} from unnest(generate_array(1, {rows})) as i"
    return f"select i::bigint as id, {body} from generate_series(1, {rows}) as s(i)"


#: Keys at both ends of INT64, so the key span is 2^64 and the bucket
#: expression's span literal cannot be an INT64 (see `int_literal`).
EXTREME_KEYS = [-(2**63), -1, 0, 1, 2**63 - 1]


# ------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module")
def rw(bigquery_url):
    """A read-write client for building fixtures, and the cleanup at the end."""
    from google.cloud import bigquery

    project, dataset, location = parse_bigquery_url(bigquery_url)
    client = bigquery.Client(project=project, location=location)
    made: list[str] = []

    def run(sql: str) -> None:
        """Run fixture DDL/DML with the read-write client."""
        client.query_and_wait(sql)

    def table(base: str) -> str:
        """A per-run table name in the test dataset, remembered for cleanup."""
        name = f"{base}_{RUN}"
        made.append(name)
        return name

    yield run, table, f"`{project}`.`{dataset}`", dataset
    for name in made:
        client.delete_table(f"{project}.{dataset}.{name}", not_found_ok=True)
    client.close()


@pytest.fixture(scope="module")
def duck(tmp_path_factory) -> str:
    """Side B: the DuckDB reference tables, built once and opened read-only."""
    path = str(tmp_path_factory.mktemp("bigquery_it") / "b.duckdb")
    con = duckdb_write(path)
    try:
        con.execute(DUCKDB_ORDERS)
        con.execute(f"create table wide as {_wide_select(200, bigquery=False)}")
        keys = ", ".join(f"(({k})::bigint, 'x{i}')" for i, k in enumerate(EXTREME_KEYS))
        con.execute(f"create table extreme as select * from (values {keys}) t(id, v)")
    finally:
        con.close()
    return path


@pytest.fixture(scope="module")
def orders(rw) -> str:
    """The BigQuery `orders` base table, identical to DuckDB's."""
    run, table, ds, _ = rw
    name = table("orders")
    run(f"create or replace table {ds}.`{name}` as {BIGQUERY_ORDERS}")
    return name


def _planted(rw, orders: str, base: str, *dml: str) -> str:
    """A copy of `orders` with planted defects applied, on the BigQuery side."""
    run, table, ds, _ = rw
    name = table(base)
    run(f"create or replace table {ds}.`{name}` as select * from {ds}.`{orders}`")
    for statement in dml:
        run(statement.format(t=f"{ds}.`{name}`"))
    return name


def _diff(url: str, duck: str, a_table: str, b_table: str = "main.orders", **kw):
    """Diff a BigQuery table against a DuckDB one, with a deep enough walk that
    the bucket expression runs at several levels rather than one fetch."""
    a = open_bigquery(url, side="A")
    b = open_duckdb(duck, side="B")
    kw.setdefault("key", "id")
    kw.setdefault("threshold", 64)
    kw.setdefault("bisection_factor", 8)
    try:
        return diff(a, b, a_table, b_table, **kw)
    finally:
        a.close()
        b.close()


# --------------------------------------------------------------------- tests


def test_the_hash_constant_agrees_with_the_other_engines(bigquery_url):
    """The cross-engine contract in one number - a fifth path to it, through
    TO_HEX(MD5()) and a 0x-hex cast to INT64 - and a near-miss input that must
    not reach it, so a constant-returning bug cannot pass."""
    a = open_bigquery(bigquery_url)
    try:
        got = a.query(
            f"select {a.hash_expr(chr(39) + 'abc' + chr(39))}, "
            f"{a.hash_expr(chr(39) + 'abd' + chr(39))}"
        )[0]
        assert got[0] == 648541476951500027
        assert got[1] != got[0]
    finally:
        a.close()


def test_every_row_renders_byte_identically_to_duckdb(bigquery_url, duck, orders, rw):
    """The correctness contract itself: per-row canonical text and the
    full-table checksum agree exactly - and a one-cent change moves both."""
    a = open_bigquery(bigquery_url, side="A")
    b = open_duckdb(duck, side="B")
    try:
        cols_a = {c.name: c for c in a.columns(orders)}
        cols_b = {c.name: c for c in b.columns("main.orders")}
        assert list(cols_a) == list(cols_b)
        key_a, key_b = a.key_spec(orders, "id"), b.key_spec("main.orders", "id")
        rest_a = [c for n, c in cols_a.items() if n != "id"]
        rest_b = [c for n, c in cols_b.items() if n != "id"]

        rows_a = a.fetch_range(orders, key_a, rest_a, 1, N + 1)
        rows_b = b.fetch_range("main.orders", key_b, rest_b, 1, N + 1)
        assert len(rows_a) == len(rows_b) == N
        bad = [k for k in rows_b if rows_a[k] != rows_b[k]]
        assert not bad, (
            f"rows differ, first: {bad[0]}: {rows_a[bad[0]]} vs {rows_b[bad[0]]}"
        )
        # Spot-check the cases most likely to drift, by value, not just by
        # agreement - two engines can agree on a wrong answer.
        assert rows_a[5][3] == "Infinity" and rows_a[6][3] == "-Infinity"
        assert rows_a[7][3] == "NaN" and rows_a[8][3] == "0.000000"
        assert rows_a[19][3] == "\\N"
        assert rows_a[1][3] == "0.142857" and rows_a[1][2] == "0.070000"
        assert rows_a[1][6] == "0999-12-26"
        assert rows_a[17][5] == "\\N"  # a NULL boolean, not 'false'

        sums_a = a.segment_checksums(orders, key_a, rest_a, 1, N + 1, 1)
        sums_b = b.segment_checksums("main.orders", key_b, rest_b, 1, N + 1, 1)
        assert sums_a == sums_b

        changed = _planted(
            rw,
            orders,
            "orders_cent",
            "update {t} set amount = amount + 0.01 where id = 4321",
        )
        moved = a.segment_checksums(changed, key_a, rest_a, 1, N + 1, 1)
        assert moved[0][0] == sums_b[0][0] and moved[0][1] != sums_b[0][1]
    finally:
        a.close()
        b.close()


def test_identical_tables_match_and_download_nothing(bigquery_url, duck, orders):
    """The headline claim, cross-engine: agreement moves zero rows - with the
    instant and naive timestamp columns raising no timezone warning, because
    BigQuery's TIMESTAMP is marked tz-aware rather than guessed from its name."""
    result = _diff(bigquery_url, duck, orders)
    assert result.identical
    assert result.diffs == []
    assert result.stats.rows_downloaded == 0
    assert not [w for w in result.warnings if "timezone" in w], result.warnings


PLANTS = {
    "changed_decimal": (
        ["update {t} set amount = amount + 0.01 where id = 1234"],
        [(1234, "different", ["amount"])],
    ),
    "deleted_row": (["delete from {t} where id = 777"], [(777, "only_in_b", None)]),
    "inserted_row": (
        ["insert into {t} (id, code, status) values (999999999, 'k999999999', 'new')"],
        [(999999999, "only_in_a", None)],
    ),
    # id 13 is a multiple of 13, so `note` is NULL on the DuckDB side.
    "null_versus_empty": (
        ["update {t} set note = '' where id = 13"],
        [(13, "different", ["note"])],
    ),
    # id 2 is FALSE on the DuckDB side (2 % 11 != 0, 2 % 17 != 0).
    "null_versus_false": (
        ["update {t} set is_refunded = null where id = 2"],
        [(2, "different", ["is_refunded"])],
    ),
}


@pytest.mark.parametrize("plant", sorted(PLANTS))
def test_each_planted_difference_is_found_exactly(bigquery_url, duck, orders, rw, plant):
    """Every planted defect is reported as that row and that column, and
    nothing else is - a parity tool that misses one, or invents one, is broken."""
    dml, expected = PLANTS[plant]
    table = _planted(rw, orders, f"orders_{plant}", *dml)
    result = _diff(bigquery_url, duck, table)
    assert len(result.diffs) == len(expected)
    # A one-sided row has no "changed columns" to check (None in PLANTS).
    got = [
        (d.key, d.kind, d.columns if cols is not None else None)
        for d, (_, _, cols) in zip(result.diffs, expected, strict=True)
    ]
    assert got == expected
    assert not result.identical


def test_a_text_key_is_hashed_and_reported_by_its_real_value(
    bigquery_url, duck, orders, rw
):
    """A STRING key goes through the hashed-bucket path; the difference must
    still come back under the key's real text, not its hash."""
    table = _planted(
        rw, orders, "orders_textkey", "update {t} set status = 'gone' where id = 4242"
    )
    result = _diff(bigquery_url, duck, table, key="code")
    assert [(d.key, d.kind, d.columns) for d in result.diffs] == [
        ("k4242", "different", ["status"])
    ]


def test_a_table_wider_than_the_nesting_limit_agrees(bigquery_url, duck, rw):
    """130 columns: BigQuery joins them flat while DuckDB's side is identical
    text by construction - so they must agree, and one changed cell deep in the
    row must be found as exactly that cell."""
    run, table, ds, _ = rw
    wide = table("wide")
    run(f"create or replace table {ds}.`{wide}` as {_wide_select(200, bigquery=True)}")
    clean = _diff(bigquery_url, duck, wide, "main.wide", threshold=16)
    assert clean.identical and clean.stats.rows_downloaded == 0

    run(f"update {ds}.`{wide}` set c99 = c99 + 1 where id = 150")
    result = _diff(bigquery_url, duck, wide, "main.wide", threshold=16)
    assert [(d.key, d.kind, d.columns) for d in result.diffs] == [
        (150, "different", ["c99"])
    ]


def test_keys_at_both_ends_of_int64(bigquery_url, duck, rw):
    """A key span of 2^64 does not fit an INT64 literal, which BigQuery refuses
    to parse; the walk must still complete, and still find a change on the
    largest key, the one an off-by-one at the top of the range would skip."""
    run, table, ds, _ = rw
    ext = table("extreme")
    rows = ", ".join(
        f"struct({k} as id, 'x{i}' as v)" for i, k in enumerate(EXTREME_KEYS)
    )
    run(f"create or replace table {ds}.`{ext}` as select * from unnest([{rows}])")
    clean = _diff(
        bigquery_url, duck, ext, "main.extreme", threshold=1, bisection_factor=2
    )
    assert clean.identical

    run(f"update {ds}.`{ext}` set v = 'changed' where id = {2**63 - 1}")
    result = _diff(
        bigquery_url, duck, ext, "main.extreme", threshold=1, bisection_factor=2
    )
    assert [(d.key, d.kind) for d in result.diffs] == [(2**63 - 1, "different")]


def test_a_live_table_is_read_at_one_snapshot(bigquery_url, rw):
    """CLAUDE.md 4.9: a row committed mid-walk must not be seen by later
    levels. A pinned dialect keeps its count; a fresh one sees the new row -
    the negative control proving the insert really landed."""
    run, table, ds, _ = rw
    live = table("live")
    run(
        f"create or replace table {ds}.`{live}` as "
        "select i as id from unnest(generate_array(1, 100)) as i"
    )
    a = open_bigquery(bigquery_url)
    try:
        key = a.key_spec(live, "id")
        assert a.key_stats(live, key).rows == 100
        run(f"insert into {ds}.`{live}` (id) values (101)")
        assert a.key_stats(live, key).rows == 100
    finally:
        a.close()
    fresh = open_bigquery(bigquery_url)
    try:
        assert fresh.key_stats(live, fresh.key_spec(live, "id")).rows == 101
    finally:
        fresh.close()


def test_a_view_is_not_pinned_and_says_so_by_behaving_live(bigquery_url, rw):
    """The documented limitation, pinned as a test: BigQuery ignores time
    travel on a view, so the dialect does not pretend to pin one. If BigQuery
    ever starts honouring it, this fails and the docs can be tightened."""
    run, table, ds, _ = rw
    base, view = table("vbase"), table("vview")
    run(f"create or replace table {ds}.`{base}` as select 1 as id")
    run(f"create or replace view {ds}.`{view}` as select * from {ds}.`{base}`")
    a = open_bigquery(bigquery_url)
    try:
        key = a.key_spec(view, "id")
        assert "for system_time" not in a.qualify(view)
        # RT-D7: and the output says so.
        assert any("read live" in n for n in a.caveats(list(key.columns)))
        assert a.key_stats(view, key).rows == 1
        run(f"insert into {ds}.`{base}` (id) values (2)")
        assert a.key_stats(view, key).rows == 2
    finally:
        a.close()


def test_the_dialect_cannot_write_even_if_asked(bigquery_url, rw):
    """Read-only by construction: the dialect's token carries only the
    bigquery.readonly scope, so the *server* refuses DDL and DML from it."""
    run, table, ds, _ = rw
    target = table("must_not_exist")
    a = open_bigquery(bigquery_url)
    try:
        with pytest.raises(Exception, match="scope"):
            a.query(f"create table {ds}.`{target}` as select 1 as x")
    finally:
        a.close()
    probe = open_bigquery(bigquery_url)
    try:
        with pytest.raises(ValueError, match="table not found"):
            probe.columns(target)
    finally:
        probe.close()


def test_a_case_mismatched_table_name_gets_a_near_miss_hint(bigquery_url, orders):
    """Table names are case-sensitive in BigQuery; the error must say which
    side, and point at the table the user probably meant."""
    a = open_bigquery(bigquery_url)
    try:
        with pytest.raises(ValueError) as exc:
            a.columns(orders.upper())
        message = str(exc.value)
        assert "side A" in message and "bigquery" in message
        assert orders in message
    finally:
        a.close()
