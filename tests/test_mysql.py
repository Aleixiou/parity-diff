"""MySQL against DuckDB, end to end.

The third engine, and the proof that the dialect abstraction holds: this file
is the MySQL twin of `test_integration.py`, and the only MySQL-specific content
is the fixture DDL. Everything it asserts - identical tables download nothing,
planted differences are found exactly, the NULL and boolean traps are caught -
is the same contract every dialect must meet.

MySQL forced three small dialect decisions that a bug here would expose: the
row hash goes through `CONV` rather than a bit-cast, the field separator is
`char(31 using utf8mb4)` because MySQL has no `chr` and a bare `char` is
binary, and the NULL sentinel is built from a hex literal because MySQL
processes backslash escapes in string literals. Each is verified below by the
values agreeing with DuckDB rather than by inspecting the SQL.

Skips cleanly when no MySQL is reachable.
"""

from __future__ import annotations

import pytest
from conftest import duckdb_write, open_duckdb, open_mysql

from parity.engine import diff

pytestmark = pytest.mark.mysql

N = 5_000

#: The DuckDB side. `is_refunded` is a plain int, not a boolean, so it lines up
#: with MySQL's tinyint(1) - MySQL has no real boolean, and comparing a boolean
#: against a tinyint is a genuine schema difference the tool would (correctly)
#: flag, which is tested separately below rather than mixed in here.
DUCKDB_TABLE = f"""
create table orders as
select i::bigint                                             as id,
       (i % 97)::integer                                     as customer_id,
       ((i * 7 % 100000) / 100.0)::decimal(12,2)             as amount,
       case when i % 3 = 0 then 'paid'
            when i % 3 = 1 then 'open' else 'void' end       as status,
       (i % 11 = 0)::int                                     as is_refunded,
       (timestamp '2024-01-01 00:00:00'
            + (i % 86400) * interval '1 second')             as created_at,
       case when i % 13 = 0 then null
            else 'note ' || i::varchar end                   as note
from generate_series(1, {N}) as s(i)
"""

MYSQL_TABLE = """
create table orders (
    id bigint primary key,
    customer_id int,
    amount decimal(12,2),
    status varchar(10),
    is_refunded int,
    created_at datetime(6),
    note varchar(50)
)
"""

MYSQL_FILL = f"""
insert into orders
select i, (i % 97), ((i * 7 % 100000) / 100.0),
       case when i % 3 = 0 then 'paid'
            when i % 3 = 1 then 'open' else 'void' end,
       (i % 11 = 0),
       timestamp('2024-01-01 00:00:00') + interval (i % 86400) second,
       case when i % 13 = 0 then null else concat('note ', i) end
from (
    with recursive seq(i) as (
        select 1 union all select i + 1 from seq where i < {N}
    ) select i from seq
) s
"""


@pytest.fixture(scope="module")
def duck_path(tmp_path_factory) -> str:
    """Side B: the DuckDB reference table, built once and opened read-only."""
    path = str(tmp_path_factory.mktemp("mysql_it") / "b.duckdb")
    con = duckdb_write(path)
    try:
        con.execute(DUCKDB_TABLE)
    finally:
        con.close()
    return path


def _build_mysql(mysql_url: str, plant: str | None) -> str:
    """Create the MySQL `orders` table, optionally with one planted defect.

    Returns the table name. Uses its own connection and commits, because the
    read-only dialect opened later must see committed data.
    """
    dialect = open_mysql(mysql_url, side="A")
    cur = dialect._conn.cursor()
    # The row generator recurses N times; MySQL caps that at 1001 by default.
    cur.execute("set session cte_max_recursion_depth = 1000000")
    cur.execute("drop table if exists orders")
    cur.execute(MYSQL_TABLE)
    cur.execute(MYSQL_FILL)
    if plant == "changed":
        cur.execute("update orders set amount = amount + 0.01 where id = 1234")
    elif plant == "deleted":
        cur.execute("delete from orders where id = 777")
    elif plant == "null_trap":
        cur.execute("update orders set note = '' where id = 13")
    dialect._conn.commit()
    dialect.close()
    return "orders"


def _diff(mysql_url: str, duck_path: str, **kwargs):
    """Diff the MySQL orders table against the DuckDB one."""
    a = open_mysql(mysql_url, side="A")
    b = open_duckdb(duck_path, side="B")
    try:
        return diff(a, b, "orders", "main.orders", "id", **kwargs)
    finally:
        a.close()
        b.close()


def test_the_hash_constant_agrees_with_the_other_engines(mysql_url):
    """The whole cross-engine contract in one number.

    MySQL reaches it through `CONV(hex, 16, 10)`, an entirely different path
    from PostgreSQL's bit-cast and DuckDB's hex-cast. If this disagrees,
    nothing else can be trusted.
    """
    a = open_mysql(mysql_url, side="A")
    try:
        got = a.query(f"select {a.hash_expr(chr(39) + 'abc' + chr(39))}")[0][0]
        assert int(got) == 648541476951500027
    finally:
        a.close()


def test_the_session_is_utc_and_snapshot_isolated(mysql_url):
    """The two guarantees a live source table depends on."""
    a = open_mysql(mysql_url, side="A")
    try:
        assert a.query("select @@session.time_zone")[0][0] == "+00:00"
        assert a.query("select @@transaction_isolation")[0][0] == "REPEATABLE-READ"
    finally:
        a.close()


def test_identical_tables_across_engines_download_nothing(mysql_url, duck_path):
    """The headline claim, MySQL to DuckDB: agreement moves no rows."""
    _build_mysql(mysql_url, plant=None)
    result = _diff(mysql_url, duck_path)

    assert result.identical, [
        (d.key, d.columns, d.values_a, d.values_b) for d in result.diffs[:5]
    ]
    assert result.stats.rows_downloaded == 0
    assert result.stats.queries == 4
    # No spurious type or timezone warnings on a clean, aligned schema.
    assert not result.warnings, result.warnings


@pytest.mark.parametrize(
    "plant,key,kind,column",
    [
        ("changed", 1234, "different", "amount"),
        ("deleted", 777, "only_in_b", None),
        ("null_trap", 13, "different", "note"),
    ],
)
def test_each_planted_difference_is_found_exactly(
    mysql_url, duck_path, plant, key, kind, column
):
    """A changed value, a deleted row, and the NULL-versus-empty-string trap.

    The trap is the one that matters most: MySQL stores `''` and DuckDB keeps
    NULL, and they must be reported as different - the migration bug class a
    naive tool passes over.
    """
    _build_mysql(mysql_url, plant=plant)
    result = _diff(mysql_url, duck_path)

    assert [(d.key, d.kind) for d in result.diffs] == [(key, kind)], (
        f"{plant}: got {[(d.key, d.kind) for d in result.diffs]}"
    )
    if column:
        assert result.diffs[0].columns == [column]
    if plant == "null_trap":
        assert result.diffs[0].values_a == {"note": ""}
        assert result.diffs[0].values_b == {"note": "\\N"}


def test_a_boolean_against_a_tinyint_is_flagged_not_hidden(mysql_url, tmp_path):
    """MySQL has no real boolean, so a `boolean` column elsewhere and a
    `tinyint(1)` here render differently ('true' versus '1').

    That is a real schema difference, and the tool must surface it rather than
    drown the reader in a false diff on every row without explanation.
    """
    duck_path = str(tmp_path / "boolcmp.duckdb")
    con = duckdb_write(duck_path)
    con.execute(
        "create table flags as select i::bigint id, (i % 2 = 0) flag "
        "from generate_series(1, 20) s(i)"
    )
    con.close()

    a = open_mysql(mysql_url, side="A")
    cur = a._conn.cursor()
    cur.execute("drop table if exists flags")
    cur.execute("create table flags (id bigint primary key, flag tinyint(1))")
    cur.execute(
        "insert into flags select i, (i % 2 = 0) from ("
        "with recursive s(i) as (select 1 union all select i+1 from s where i<20) "
        "select i from s) x"
    )
    a._conn.commit()
    b = open_duckdb(duck_path, side="B")
    try:
        result = diff(a, b, "flags", "main.flags", "id")
        assert any("flag" in w and "tinyint" in w.lower() for w in result.warnings), (
            result.warnings
        )
    finally:
        a.close()
        b.close()
