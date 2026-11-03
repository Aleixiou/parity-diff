"""The SQL a filtered run emits, on every dialect, with no database.

`--where` is pasted into the WHERE clause of every query that reads the
table. One query that missed it would compare rows the others never saw - a
false match or an unexplained result - so this file drives each dialect's four
row-reading queries with a filter, records every statement, and asserts the
parenthesised predicate is in each one that names the table, ANDed ahead of
the tool's own conditions. The live suites (`test_where_live.py`) then prove
the same SQL runs and finds planted differences.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from parity.dialects.bigquery_dialect import BigQueryDialect
from parity.dialects.duckdb_dialect import DuckDBDialect
from parity.dialects.mssql_dialect import SQLServerDialect, is_single_select
from parity.dialects.mysql_dialect import MySQLDialect
from parity.dialects.postgres_dialect import PostgresDialect
from parity.dialects.snowflake_dialect import SnowflakeDialect
from parity.types import Column, KeySpec, LogicalType

WHERE = "region = 'eu' or region = 'ch'"
WRAPPED = f"({WHERE})"
TS = "2026-11-02 03:31:10.559880+00"


def recording(cls: type) -> Any:
    """A dialect of `cls` whose `query` records the SQL and answers with
    the shapes the four row-reading queries expect."""

    class Recording(cls):  # type: ignore[misc, valid-type]
        def query(self, sql: str) -> list[tuple[Any, ...]]:
            """Record the SQL; answer the key stats with one row, else none."""
            self.sql.append(sql)
            if sql.startswith("select min("):
                return [(1, 100, 100, 100, 100)]
            return []

    d = Recording(side="A")
    d.sql = []
    if cls is BigQueryDialect:
        d.project, d.default_schema = "proj", "ds"
        # A base table pinned to a snapshot, as `columns()` leaves it: the
        # filter has to combine with FOR SYSTEM_TIME AS OF.
        d.snapshot = TS
        d._pinned.add(("proj", "ds", "orders"))
    if cls is SQLServerDialect:
        d.database = "wh"
    return d


DIALECTS = [
    PostgresDialect,
    DuckDBDialect,
    MySQLDialect,
    SnowflakeDialect,
    BigQueryDialect,
    SQLServerDialect,
]


def _drive(d: Any, key: KeySpec, cols: list[Column]) -> None:
    """Every query that reads the table's rows, filtered."""
    d.key_stats("orders", key, WHERE)
    d.segment_checksums("orders", key, cols, 1, 101, 8, WHERE)
    d.fetch_range("orders", key, cols, 1, 13, WHERE)
    d.fetch_sample("orders", key, cols, 7, 3, WHERE)
    d.fetch_sample("orders", key, cols, 1, 0, WHERE)


@pytest.mark.parametrize("cls", DIALECTS, ids=lambda c: c.__name__)
@pytest.mark.parametrize("hashed", [False, True], ids=["integer key", "hashed key"])
def test_every_row_reading_query_carries_the_filter(cls, hashed):
    """Five statements, each naming the table once, each filtered once, the
    filter first and ANDed - so `or` cannot widen the bucket bounds."""
    d = recording(cls)
    if hashed:
        key = KeySpec((Column("code", LogicalType.STRING, "varchar"),), True)
    else:
        key = KeySpec((Column("id", LogicalType.INTEGER, "bigint"),), False)
    cols = [
        Column("amount", LogicalType.DECIMAL, "decimal(12,2)"),
        Column("region", LogicalType.STRING, "varchar"),
    ]
    _drive(d, key, cols)
    assert len(d.sql) == 5
    table = d.qualify("orders")
    for sql in d.sql:
        assert sql.count(table) == 1, sql
        assert sql.count(WRAPPED) == 1, sql
        # The filter is the first condition, straight after the table.
        assert f"{table} where {WRAPPED}" in sql, sql
        after = sql.split(WRAPPED, 1)[1]
        # Whatever follows it is the tool's own, joined by AND (or nothing:
        # the key stats and an unsampled fetch have no condition of their own).
        assert re.match(r"\s*($|and |group by |order by |option )", after), after
        if cls is SQLServerDialect:
            # The SELECT guard must still accept every filtered query.
            assert is_single_select(sql), sql


def test_the_bigquery_filter_follows_the_time_travel_clause():
    """`FROM t FOR SYSTEM_TIME AS OF ... WHERE (...)`: the snapshot clause
    belongs to the table reference, the filter to the WHERE."""
    d = recording(BigQueryDialect)
    key = KeySpec((Column("id", LogicalType.INTEGER, "INT64"),), False)
    d.key_stats("orders", key, WHERE)
    assert (
        f"from `proj`.`ds`.`orders` for system_time as of timestamp '{TS}' "
        f"where {WRAPPED}"
    ) in d.sql[0]


@pytest.mark.parametrize("cls", DIALECTS, ids=lambda c: c.__name__)
def test_an_unfiltered_query_is_byte_for_byte_what_it_was(cls):
    """No filter, no `where` keyword added and no parentheses: the SQL every
    existing checksum was verified with is unchanged."""
    d = recording(cls)
    key = KeySpec((Column("id", LogicalType.INTEGER, "bigint"),), False)
    d.key_stats("orders", key)
    assert d.sql[0].endswith(f"from {d.qualify('orders')}")
    d.fetch_sample("orders", key, [], 1, 0)
    assert d.sql[1].endswith(f"from {d.qualify('orders')}")


@pytest.mark.parametrize(
    "predicate",
    [
        'region = "eu"',  # T-SQL reads it as an identifier; the guard refuses `"`
        "region in (select region from dbo.x into y)",
        "1 = 1 or exec_sql = 1 and exec(1) = 1",
        "region = 'eu' and set_flag() = 1 or (select 1 set x = 1) = 1",
    ],
)
def test_sql_server_refuses_what_its_select_guard_would(predicate):
    """Refused up front, naming the side, rather than at the first query."""
    d = recording(SQLServerDialect)
    with pytest.raises(ValueError, match=r"\[side A: mssql\].*SELECT guard"):
        d.check_where(predicate)


def test_sql_server_accepts_bracketed_names_and_write_words_in_literals():
    """`[update]` and `'delete'` are a name and a value, not statements."""
    d = recording(SQLServerDialect)
    d.check_where("[update] = 'delete' and [set] > 0")
