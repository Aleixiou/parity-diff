"""The identical check between the two most common migration endpoints.

MySQL and PostgreSQL are each verified against DuckDB elsewhere, so byte-equal
canonical text makes them agree with each other by transitivity - but a
MySQL -> PostgreSQL migration is common enough that the pair deserves a direct
test, with neither side being the reference engine. Identical data built by
each engine's own dialect must read identical; a single planted change must be
found exactly.

Skips unless both a MySQL and a PostgreSQL server are reachable.
"""

from __future__ import annotations

import pytest
from conftest import PG_SCHEMA, open_mysql, open_pg

from parity.engine import diff

pytestmark = [pytest.mark.postgres, pytest.mark.mysql]

N = 2_000
SCHEMA = f"{PG_SCHEMA}_mypg"

# The same rows, spelled in each engine's own SQL. `flag` is a plain int on both
# (not a boolean) so this tests data agreement, not the boolean-vs-int trap.
PG_BUILD = f"""
create table {SCHEMA}.xeng as
select i::bigint                                            as id,
       ((i * 7 % 100000) / 100.0)::decimal(12,2)            as amount,
       (case when i % 3 = 0 then 'paid'
             when i % 3 = 1 then 'open' else 'void' end)::varchar(10) as status,
       (i % 11 = 0)::int                                    as flag,
       (case when i % 13 = 0 then null
             else 'n' || i::text end)::varchar(20)          as note
from generate_series(1, {N}) as s(i)
"""

MYSQL_BUILD = f"""
create table xeng as
with recursive seq(i) as (
    select 1 union all select i + 1 from seq where i < {N}
)
select cast(i as signed)                                   as id,
       cast((i * 7 mod 100000) / 100.0 as decimal(12,2))   as amount,
       (case when i mod 3 = 0 then 'paid'
             when i mod 3 = 1 then 'open' else 'void' end)  as status,
       cast(i mod 11 = 0 as signed)                        as flag,
       (case when i mod 13 = 0 then null
             else concat('n', i) end)                       as note
from seq
"""


@pytest.fixture(scope="module")
def pg(pg_url):
    """Side A: PostgreSQL, its own schema, opened read-only."""
    import psycopg

    con = psycopg.connect(pg_url, autocommit=True)
    try:
        con.execute(f"drop schema if exists {SCHEMA} cascade")
        con.execute(f"create schema {SCHEMA}")
        con.execute(PG_BUILD)
    finally:
        con.close()
    d = open_pg(pg_url, side="A")
    yield d
    d.close()


@pytest.fixture(scope="module")
def my(mysql_url):
    """Side B: MySQL, identical data, opened read-only."""
    d = open_mysql(mysql_url, side="B")
    cur = d._conn.cursor()
    try:
        cur.execute("set session cte_max_recursion_depth = 1000000")
        cur.execute("drop table if exists xeng")
        cur.execute("drop table if exists xeng_p")
        cur.execute(MYSQL_BUILD)
        # A perturbed copy for the planted-difference test, built once.
        cur.execute("create table xeng_p as select * from xeng")
        cur.execute("update xeng_p set amount = amount + 0.01 where id = 1234")
        d._conn.commit()
    finally:
        cur.close()
    yield d
    d.close()


def test_mysql_and_postgres_agree_on_identical_data(pg, my):
    """The same rows on each engine read identical, with zero download."""
    result = diff(pg, my, f"{SCHEMA}.xeng", "xeng", "id")
    assert result.identical, (
        "MySQL and PostgreSQL disagreed on identical data: "
        f"{[(d.key, d.columns, d.values_a, d.values_b) for d in result.diffs[:5]]}"
    )
    assert result.stats.rows_downloaded == 0


def test_a_planted_change_between_mysql_and_postgres_is_found(pg, my):
    """One changed decimal is reported as exactly that row and column."""
    result = diff(pg, my, f"{SCHEMA}.xeng", "xeng_p", "id")
    assert [(d.key, d.kind) for d in result.diffs] == [(1234, "different")]
    assert result.diffs[0].columns == ["amount"]
