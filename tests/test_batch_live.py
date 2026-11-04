"""`parity run` against real engines: one config mixing PostgreSQL↔DuckDB and
MySQL↔DuckDB pairs, with planted differences, a `where` and a `map` per pair.

Side B is always one DuckDB file. Side A's data comes from the same row
generator, so every table is identical by construction until planted:

- `planted` (PostgreSQL and MySQL): key 3 (region `us`) and key 4 (region
  `eu`) have a changed amount;
- DuckDB `renamed` carries `orders` under other names (`order_id`, `amt`,
  `cust_nm`) with one planted change in `cust_nm` at key 77.

Skips unless both PostgreSQL and MySQL are reachable.
"""

from __future__ import annotations

import io
import json
import xml.etree.ElementTree as ET

import pytest
from conftest import PG_SCHEMA, duckdb_write, mysql_rw_cursor, open_mysql

from parity import batch
from parity.cli import EXIT_DIFFERENCES, build_parser

N = 2_000
SCHEMA = f"{PG_SCHEMA}_batch"

SELECT = """
select i::bigint                                             as id,
       case when i % 2 = 0 then 'eu' else 'us' end           as region,
       ((i * 7 % 10000) / 100.0)::decimal(12,2)              as amount,
       'cust ' || i::varchar                                 as customer_name,
       'note ' || (i % 5)::varchar                           as note
from generate_series(1, {n}) as s(i)
"""

PLANT = [
    "update {t} set amount = amount + 1 where id = 3",
    "update {t} set amount = amount + 1 where id = 4",
]

MYSQL_TABLE = """
create table {t} (
    id bigint primary key,
    region varchar(10),
    amount decimal(12,2),
    customer_name varchar(40),
    note varchar(40)
)
"""

MYSQL_FILL = f"""
insert into {{t}}
select i, case when i % 2 = 0 then 'eu' else 'us' end,
       ((i * 7 % 10000) / 100.0), concat('cust ', i), concat('note ', i % 5)
from (
    with recursive seq(i) as (
        select 1 union all select i + 1 from seq where i < {N}
    ) select i from seq
) s
"""


@pytest.fixture(scope="module")
def duck(tmp_path_factory) -> str:
    """Side B: the pristine orders and their renamed twin with one plant."""
    path = str(tmp_path_factory.mktemp("batch") / "b.duckdb")
    con = duckdb_write(path)
    try:
        con.execute(f"create table orders as {SELECT.format(n=N)}")
        con.execute(
            "create table renamed as select id as order_id, region, amount as amt, "
            "customer_name as cust_nm, note from orders"
        )
        con.execute("update renamed set cust_nm = 'someone else' where order_id = 77")
    finally:
        con.close()
    return path.replace("\\", "/")


@pytest.fixture(scope="module")
def pg(pg_url) -> str:
    """Side A on PostgreSQL: a planted and a clean table."""
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
    return pg_url


@pytest.fixture(scope="module")
def mysql(mysql_url) -> str:
    """Side A on MySQL: a planted and a clean table."""
    d = open_mysql(mysql_url, side="A")
    cur = mysql_rw_cursor(d)
    cur.execute("set session cte_max_recursion_depth = 1000000")
    for name in ("batch_planted", "batch_clean"):
        cur.execute(f"drop table if exists {name}")
        cur.execute(MYSQL_TABLE.format(t=name))
        cur.execute(MYSQL_FILL.format(t=name))
    for sql in PLANT:
        cur.execute(sql.format(t="batch_planted"))
    d._conn.commit()
    d.close()
    return mysql_url


RENAMES = '{ id = "order_id", amount = "amt", customer_name = "cust_nm" }'

CONFIG = f"""
[connections]
legacy_pg = "${{PARITY_BATCH_PG}}"
legacy_mysql = "${{PARITY_BATCH_MYSQL}}"
new = "duckdb:///{{duck}}"

[defaults]
threshold = 16
verify_sample = 500

[[pair]]
name = "pg-eu"
a = "legacy_pg"
b = "new"
a_table = "{SCHEMA}.planted"
b_table = "main.orders"
key = "id"
where = "region = 'eu'"

[[pair]]
name = "pg-all"
a = "legacy_pg"
b = "new"
a_table = "{SCHEMA}.planted"
b_table = "main.orders"
key = "id"

[[pair]]
name = "pg-renamed"
a = "legacy_pg"
b = "new"
a_table = "{SCHEMA}.clean"
b_table = "main.renamed"
key = "id"
map = {RENAMES}

[[pair]]
name = "mysql-eu"
a = "legacy_mysql"
b = "new"
a_table = "batch_planted"
b_table = "main.orders"
key = "id"
a_where = "region = 'eu'"
b_where = "region = 'eu'"

[[pair]]
name = "mysql-clean"
a = "legacy_mysql"
b = "new"
a_table = "batch_clean"
b_table = "main.orders"
key = "id"

[[pair]]
name = "mysql-renamed"
a = "legacy_mysql"
b = "new"
a_table = "batch_clean"
b_table = "main.renamed"
key = "id"
map = {RENAMES}
"""


@pytest.mark.postgres
@pytest.mark.mysql
@pytest.mark.duckdb
def test_a_mixed_config_finds_exactly_the_planted_differences(pg, mysql, duck, tmp_path):
    """Every pair reports exactly its planted rows; the clean pair is
    identical; every report carries the filters and renames; no output
    holds either connection string; exit 1."""
    path = tmp_path / "migration.toml"
    path.write_text(CONFIG.replace("{duck}", duck), encoding="utf-8")
    md, junit = str(tmp_path / "r.md"), str(tmp_path / "r.xml")
    args = build_parser().parse_args(
        ["run", str(path), "--json", "--markdown", md, "--junit", junit]
    )
    out, err = io.StringIO(), io.StringIO()
    env = {"PARITY_BATCH_PG": pg, "PARITY_BATCH_MYSQL": mysql}
    code = batch.run_command(args, out, err, environ=env)
    data = json.loads(out.getvalue())
    errors = {p["name"]: p["error"] for p in data["pairs"] if p["error"]}
    assert errors == {}
    assert code == EXIT_DIFFERENCES

    found = {
        p["name"]: sorted(
            (d["key"], d["kind"], d["columns"]) for d in p["result"]["differences"]
        )
        for p in data["pairs"]
    }
    amount = ["amount"]
    assert found == {
        "pg-eu": [(4, "different", amount)],
        "pg-all": [(3, "different", amount), (4, "different", amount)],
        "pg-renamed": [(77, "different", ["customer_name"])],
        "mysql-eu": [(4, "different", amount)],
        "mysql-clean": [],
        "mysql-renamed": [(77, "different", ["customer_name"])],
    }
    status = {p["name"]: p["status"] for p in data["pairs"]}
    assert status["mysql-clean"] == "identical"
    assert data["totals"]["identical"] == 1 and data["totals"]["differ"] == 5

    by_name = {p["name"]: p["result"] for p in data["pairs"]}
    assert by_name["pg-eu"]["where_a"] == by_name["pg-eu"]["where_b"] == "region = 'eu'"
    assert by_name["mysql-renamed"]["column_map"] == {
        "id": "order_id",
        "amount": "amt",
        "customer_name": "cust_nm",
    }
    report = (tmp_path / "r.md").read_text("utf-8")
    assert "A where region = 'eu'; B where region = 'eu'" in report
    assert "customer\\_name = cust\\_nm" in report
    suite = ET.parse(junit).getroot().find("testsuite")  # noqa: S314 - our own output
    assert (suite.get("tests"), suite.get("failures"), suite.get("errors")) == (
        "6",
        "5",
        "0",
    )
    for text in (
        out.getvalue(),
        err.getvalue(),
        report,
        (tmp_path / "r.xml").read_text(),
    ):
        assert pg not in text and mysql not in text
