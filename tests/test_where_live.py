"""`--where` and `--map` against real engines, with planted differences.

Side B is always DuckDB; side A is PostgreSQL, MySQL or SQL Server (each
skips when its engine is not reachable). The data is built from one row
generator, so it is identical by construction, and then planted:

- key 3 (region `us`) has a changed amount: **outside** the filter
  `region = 'eu'`, so a filtered run must not report it;
- key 4 (region `eu`) has a changed amount: inside, so it must be found;
- key 6 had its region changed on side A to `xx`: it leaves A's filter and
  stays in B's, so it reads as only in B.

The rename tables carry the same data under other names on side B
(`order_id`, `amt`, `cust_nm`) with one planted change in a renamed column.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from conftest import (
    PG_SCHEMA,
    duckdb_write,
    mysql_rw_cursor,
    open_duckdb,
    open_mssql,
    open_mysql,
    open_pg,
)

from parity.cli import EXIT_DIFFERENCES, EXIT_ERROR, EXIT_IDENTICAL, main
from parity.engine import diff

N = 2_000
EU = "region = 'eu'"
SCHEMA = f"{PG_SCHEMA}_where"

#: Spelled identically on PostgreSQL and DuckDB.
SELECT = """
select i::bigint                                             as id,
       case when i % 2 = 0 then 'eu' else 'us' end           as region,
       ((i * 7 % 10000) / 100.0)::decimal(12,2)              as amount,
       'cust ' || i::varchar                                 as customer_name,
       'note ' || (i % 5)::varchar                           as note
from generate_series(1, {n}) as s(i)
"""

#: The same rows, renamed the way a migration might.
RENAMED = """
select id as order_id, region, amount as amt, customer_name as cust_nm, note
from orders
"""

#: What every filtered run must find: key 4 changed, key 6 only in B.
EXPECTED = [(4, "different", ["amount"]), (6, "only_in_b", [])]

PLANT = [
    "update {t} set amount = amount + 1 where id = 3",
    "update {t} set amount = amount + 1 where id = 4",
    "update {t} set region = 'xx' where id = 6",
]


def found(result) -> list[tuple[Any, str, list[str]]]:
    """(key, kind, columns) for every reported difference."""
    return sorted(
        (d.key, d.kind, d.columns if d.kind == "different" else []) for d in result.diffs
    )


@pytest.fixture(scope="module")
def duck(tmp_path_factory) -> str:
    """Side B: the pristine table, and its renamed twin with one plant."""
    path = str(tmp_path_factory.mktemp("where") / "b.duckdb")
    con = duckdb_write(path)
    try:
        con.execute(f"create table orders as {SELECT.format(n=N)}")
        con.execute(f"create table renamed as {RENAMED}")
        con.execute("update renamed set cust_nm = 'someone else' where order_id = 77")
        # The swap trap: `customer_name` and `note` stored in each other's
        # columns, except row 88, planted.
        con.execute(
            "create table swapped as select id, region, amount, "
            "note as customer_name, customer_name as note from orders"
        )
        con.execute("update swapped set customer_name = 'planted' where id = 88")
    finally:
        con.close()
    return path


# ------------------------------------------------------------- PostgreSQL


@pytest.fixture(scope="module")
def pg_tables(pg_url) -> dict[str, str]:
    """Side A on PostgreSQL: the planted table and a clean one."""
    import psycopg

    con = psycopg.connect(pg_url, autocommit=True)
    try:
        con.execute(f"drop schema if exists {SCHEMA} cascade")
        con.execute(f"create schema {SCHEMA}")
        for name in ("planted", "clean"):
            con.execute(f"create table {SCHEMA}.{name} as {SELECT.format(n=N)}")
        for sql in PLANT:
            con.execute(sql.format(t=f"{SCHEMA}.planted"))
    finally:
        con.close()
    return {"planted": f"{SCHEMA}.planted", "clean": f"{SCHEMA}.clean"}


def pg_diff(pg_url, duck, a_table, b_table="main.orders", **kwargs):
    """Diff a PostgreSQL table against a DuckDB one, walking several levels."""
    a = open_pg(pg_url, side="A")
    b = open_duckdb(duck, side="B")
    kwargs.setdefault("threshold", 16)
    kwargs.setdefault("bisection_factor", 8)
    try:
        return diff(a, b, a_table, b_table, "id", **kwargs)
    finally:
        a.close()
        b.close()


def test_pg_a_filtered_run_finds_exactly_the_planted_differences_inside(
    pg_url, duck, pg_tables
):
    """Key 3 (outside) is not reported; key 4 and key 6 are."""
    result = pg_diff(pg_url, duck, pg_tables["planted"], where_a=EU, where_b=EU)
    assert found(result) == EXPECTED
    assert result.where_a == result.where_b == EU
    assert result.stats.rows_compared_b == N // 2


def test_pg_unfiltered_the_outside_difference_is_found_too(pg_url, duck, pg_tables):
    """The control: without the filter key 3 is a difference as well."""
    result = pg_diff(pg_url, duck, pg_tables["planted"])
    assert (3, "different", ["amount"]) in found(result)


def test_pg_a_filter_on_rows_that_differ_only_outside_it_is_identical(
    pg_url, duck, pg_tables
):
    """`id > 10` excludes every plant: the run is identical over the rows
    it compared, and says it is filtered, in human output and JSON alike."""
    where = "id > 10"
    a_url = pg_url
    b_url = f"duckdb:///{duck}"
    args = [
        "diff", "--a", a_url, "--a-table", pg_tables["planted"],
        "--b", b_url, "--b-table", "main.orders", "--key", "id", "--where", where,
    ]  # fmt: skip
    out = io.StringIO()
    assert main(args, out=out) == EXIT_IDENTICAL, out.getvalue()
    text = out.getvalue()
    assert f"no differences in {N - 10:,} rows (filtered rows only)" in text
    assert f"filtered: A where {where}; B where {where}" in text
    out = io.StringIO()
    assert main([*args, "--json"], out=out) == EXIT_IDENTICAL
    payload = json.loads(out.getvalue())
    assert payload["where_a"] == payload["where_b"] == where
    assert payload["risk"]["filtered"] is True
    assert payload["risk"]["rows"] == N - 10


def test_pg_per_side_filters_replace_where_on_their_side(pg_url, duck, pg_tables):
    """`--a-where` in PostgreSQL's spelling, `--b-where` in DuckDB's; the
    shared `--where` is overridden on both sides."""
    args = [
        "diff", "--a", pg_url, "--a-table", pg_tables["planted"],
        "--b", f"duckdb:///{duck}", "--b-table", "main.orders", "--key", "id",
        "--where", "1 = 0",
        "--a-where", "region::text = 'eu'",
        "--b-where", "cast(region as varchar) = 'eu'",
        "--json", "--threshold", "16",
    ]  # fmt: skip
    out = io.StringIO()
    assert main(args, out=out) == EXIT_DIFFERENCES
    payload = json.loads(out.getvalue())
    got = sorted((d["key"], d["kind"]) for d in payload["differences"])
    assert got == [(4, "different"), (6, "only_in_b")]
    assert payload["where_a"] == "region::text = 'eu'"


def test_pg_every_statement_that_reads_the_table_is_filtered(pg_url, duck, pg_tables):
    """Record every query both live sides run: each one that names the table
    carries the predicate. The walk goes three levels deep and downloads."""
    from parity.dialects.duckdb_dialect import DuckDBDialect
    from parity.dialects.postgres_dialect import PostgresDialect

    seen: list[tuple[str, str]] = []
    a = open_pg(pg_url, side="A")
    b = open_duckdb(duck, side="B")
    for side, d in (("A", a), ("B", b)):
        real = d.query

        def record(sql, _real=real, _side=side):
            """Remember the statement and its side, then run it."""
            seen.append((_side, sql))
            return _real(sql)

        d.query = record
    try:
        result = diff(
            a, b, pg_tables["planted"], "main.orders", "id",
            where_a=EU, where_b=EU, threshold=4, bisection_factor=4,
        )  # fmt: skip
    finally:
        a.close()
        b.close()
    assert found(result) == EXPECTED
    assert isinstance(a, PostgresDialect) and isinstance(b, DuckDBDialect)
    tables = {"A": a.qualify(pg_tables["planted"]), "B": b.qualify("main.orders")}
    reads = [(side, sql) for side, sql in seen if tables[side] in sql]
    assert len(reads) >= 8, reads
    assert any(sql.startswith("select min(") for _, sql in reads)  # key stats
    assert any("order by" in sql for _, sql in reads)  # a download
    for side, sql in reads:
        assert f"({EU})" in sql, (side, sql)


def test_pg_a_random_filter_never_reads_identical(pg_url, pg_tables):
    """`random() < 0.5` selects another half of the rows in every query, even
    inside PostgreSQL's single snapshot: on two identical tables the run is
    never identical."""
    a = open_pg(pg_url, side="A")
    b = open_pg(pg_url, side="B")
    try:
        result = diff(
            a, b, pg_tables["clean"], pg_tables["clean"], "id",
            where_a="random() < 0.5", where_b="random() < 0.5", verify_sample=0,
        )  # fmt: skip
    finally:
        a.close()
        b.close()
    assert not result.identical
    assert any("random()" in w for w in result.warnings)


def test_pg_now_inside_one_snapshot_is_stable(pg_url, duck, pg_tables):
    """`now()` is the transaction's start on PostgreSQL, so inside the
    REPEATABLE READ snapshot a filter on it selects one set of rows: the
    planted difference inside is found exactly, nothing unexplained."""
    result = pg_diff(
        pg_url, duck, pg_tables["planted"],
        where_a=f"{EU} and now() > timestamp '2000-01-01'",
        where_b=f"{EU} and now() > timestamp '2000-01-01'",
    )  # fmt: skip
    assert found(result) == EXPECTED
    assert not result.unexplained


def test_pg_a_filter_that_cannot_run_names_the_side(pg_url, duck, pg_tables):
    """A predicate naming a column that does not exist: exit 2, side named."""
    args = [
        "diff", "--a", pg_url, "--a-table", pg_tables["planted"],
        "--b", f"duckdb:///{duck}", "--b-table", "main.orders", "--key", "id",
        "--a-where", "no_such_column = 1", "--b-where", EU,
    ]  # fmt: skip
    err = io.StringIO()
    assert main(args, out=io.StringIO(), err=err) == EXIT_ERROR
    assert "[side A" in err.getvalue()


def test_a_refused_filter_exits_two_before_connecting(duck):
    """`;` is refused before any query, exit 2."""
    args = [
        "diff", "--a", f"duckdb:///{duck}", "--a-table", "main.orders",
        "--b", f"duckdb:///{duck}", "--b-table", "main.orders", "--key", "id",
        "--where", "1 = 1; select 1",
    ]  # fmt: skip
    err = io.StringIO()
    assert main(args, out=io.StringIO(), err=err) == EXIT_ERROR
    assert "contains ';'" in err.getvalue()


# ---------------------------------------------------------------- renames


def test_pg_renamed_columns_and_key_are_compared_and_a_plant_found(
    pg_url, duck, pg_tables
):
    """B renamed `id`, `amount` and `customer_name`; mapped, the clean table
    matches except key 77's renamed column, found under A's name."""
    result = pg_diff(
        pg_url, duck, pg_tables["clean"], "main.renamed",
        column_map={"id": "order_id", "amount": "amt", "customer_name": "cust_nm"},
    )  # fmt: skip
    assert found(result) == [(77, "different", ["customer_name"])]
    assert result.column_map == {
        "id": "order_id",
        "amount": "amt",
        "customer_name": "cust_nm",
    }
    assert not result.schema_differences


def test_pg_rename_through_the_cli_names_both_sides(pg_url, duck, pg_tables):
    """`--map` repeatable and comma-separated; the report shows `(B: ...)`."""
    args = [
        "diff", "--a", pg_url, "--a-table", pg_tables["clean"],
        "--b", f"duckdb:///{duck}", "--b-table", "main.renamed", "--key", "id",
        "--map", "id=order_id,amount=amt", "--map", "customer_name=cust_nm",
    ]  # fmt: skip
    out = io.StringIO()
    assert main(args, out=out) == EXIT_DIFFERENCES
    assert "customer_name (B: cust_nm)" in out.getvalue()
    out = io.StringIO()
    assert main([*args, "--json"], out=out) == EXIT_DIFFERENCES
    payload = json.loads(out.getvalue())
    assert payload["differences"][0]["b_columns"] == ["cust_nm"]


def test_pg_swapped_columns_mapped_as_a_swap_find_the_one_plant(pg_url, duck, pg_tables):
    """B stores A's `customer_name` in its `note` and A's `note` in its
    `customer_name`. Mapped as a swap, only row 88 differs: B's
    `customer_name` there was planted, and it holds A's `note`."""
    result = pg_diff(
        pg_url, duck, pg_tables["clean"], "main.swapped",
        column_map={"customer_name": "note", "note": "customer_name"},
    )  # fmt: skip
    assert found(result) == [(88, "different", ["note"])]


def test_pg_swapped_columns_unmapped_differ_on_every_row(pg_url, duck, pg_tables):
    """The control: without the map every row differs in both columns."""
    result = pg_diff(pg_url, duck, pg_tables["clean"], "main.swapped", max_diffs=None)
    assert len(result.diffs) == N
    assert all(sorted(d.columns) == ["customer_name", "note"] for d in result.diffs)


# ------------------------------------------------------------------ MySQL

MYSQL_TABLE = """
create table where_orders (
    id bigint primary key,
    region varchar(10),
    amount decimal(12,2),
    customer_name varchar(40),
    note varchar(40)
)
"""

MYSQL_FILL = f"""
insert into where_orders
select i, case when i % 2 = 0 then 'eu' else 'us' end,
       ((i * 7 % 10000) / 100.0), concat('cust ', i), concat('note ', i % 5)
from (
    with recursive seq(i) as (
        select 1 union all select i + 1 from seq where i < {N}
    ) select i from seq
) s
"""


@pytest.fixture(scope="module")
def mysql_table(mysql_url) -> str:
    """Side A on MySQL, planted."""
    d = open_mysql(mysql_url, side="A")
    cur = mysql_rw_cursor(d)
    cur.execute("set session cte_max_recursion_depth = 1000000")
    cur.execute("drop table if exists where_orders")
    cur.execute(MYSQL_TABLE)
    cur.execute(MYSQL_FILL)
    for sql in PLANT:
        cur.execute(sql.format(t="where_orders"))
    d._conn.commit()
    d.close()
    return "where_orders"


def test_mysql_a_filtered_run_finds_exactly_the_planted_differences_inside(
    mysql_url, duck, mysql_table
):
    """MySQL's own spelling of the filter, through every query."""
    a = open_mysql(mysql_url, side="A")
    b = open_duckdb(duck, side="B")
    try:
        result = diff(
            a, b, mysql_table, "main.orders", "id",
            where_a="region = 'eu'", where_b=EU, threshold=16, bisection_factor=8,
        )  # fmt: skip
    finally:
        a.close()
        b.close()
    assert found(result) == EXPECTED
    assert result.stats.rows_compared_b == N // 2


def test_mysql_a_hash_comment_is_refused(mysql_url, duck, mysql_table):
    """`#` is a comment on MySQL: refused before any query."""
    a = open_mysql(mysql_url, side="A")
    b = open_duckdb(duck, side="B")
    try:
        with pytest.raises(ValueError, match="'#'"):
            diff(a, b, mysql_table, "main.orders", "id", where_a="id > 1 # x")
    finally:
        a.close()
        b.close()


# ------------------------------------------------------------- SQL Server


@pytest.fixture(scope="module")
def mssql_table(mssql_url) -> str:
    """Side A on SQL Server, planted, in the login's own database."""
    import mssql_python

    from parity.dialects.mssql_dialect import parse_mssql_url

    odbc, _ = parse_mssql_url(mssql_url)
    con = mssql_python.connect(odbc, autocommit=True)
    cur = con.cursor()
    try:
        cur.execute(
            "if schema_id('parity_test') is null exec('create schema parity_test')"
        )
        cur.execute("drop table if exists parity_test.where_orders")
        cur.execute(
            "with n as (select top (?) cast(row_number() over (order by (select null)) "
            "as bigint) as i from sys.all_objects a cross join sys.all_objects b) "
            "select i as id, cast(case when i % 2 = 0 then 'eu' else 'us' end "
            "as varchar(10)) as region, "
            "cast((i * 7 % 10000) / 100.0 as decimal(12,2)) as amount, "
            "cast(concat('cust ', i) as varchar(40)) as customer_name, "
            "cast(concat('note ', i % 5) as varchar(40)) as note "
            "into parity_test.where_orders from n",
            (N,),
        )
        for sql in PLANT:
            cur.execute(sql.format(t="parity_test.where_orders"))
        yield "parity_test.where_orders"
        cur.execute("drop table if exists parity_test.where_orders")
    finally:
        cur.close()
        con.close()


def test_mssql_a_filtered_run_finds_exactly_the_planted_differences_inside(
    mssql_url, duck, mssql_table
):
    """T-SQL's spelling of the filter passes the SELECT guard in every query."""
    a = open_mssql(mssql_url, side="A")
    b = open_duckdb(duck, side="B")
    try:
        result = diff(
            a, b, mssql_table, "main.orders", "id",
            where_a="[region] = N'eu'", where_b=EU, threshold=16, bisection_factor=8,
        )  # fmt: skip
    finally:
        a.close()
        b.close()
    assert found(result) == EXPECTED
    assert result.stats.rows_compared_a == N // 2 - 1  # key 6 left A's filter


def test_mssql_a_double_quoted_filter_is_refused_up_front(mssql_url, duck, mssql_table):
    """The guard refuses `"`; the refusal names the filter, not a query."""
    a = open_mssql(mssql_url, side="A")
    b = open_duckdb(duck, side="B")
    try:
        with pytest.raises(ValueError, match="SELECT guard"):
            diff(a, b, mssql_table, "main.orders", "id", where_a="\"region\" = 'eu'")
    finally:
        a.close()
        b.close()
