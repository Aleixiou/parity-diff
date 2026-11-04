"""The `--where` scanner reads brackets as every engine does (RT-HB6).

`[` is a SQL Server identifier quote and an array literal or subscript
everywhere else, where a quote inside it opens a string. The scanner used to
read only the first: `[']']` ended its "identifier" at the `]` inside the
string literal, so a BigQuery predicate escaped its parentheses - and with
them the bucket bounds it is ANDed with. Now it scans under both readings
and refuses what either finds. Offline: each dialect's own `check_where`.
"""

from __future__ import annotations

import pytest

from parity.dialects.base import row_filter_problem
from parity.dialects.bigquery_dialect import BigQueryDialect
from parity.dialects.duckdb_dialect import DuckDBDialect
from parity.dialects.mssql_dialect import SQLServerDialect
from parity.dialects.mysql_dialect import MySQLDialect
from parity.dialects.oracle_dialect import OracleDialect
from parity.dialects.postgres_dialect import PostgresDialect
from parity.dialects.snowflake_dialect import SnowflakeDialect

DIALECTS = [
    PostgresDialect,
    DuckDBDialect,
    BigQueryDialect,
    MySQLDialect,
    SQLServerDialect,
    SnowflakeDialect,
    OracleDialect,
]

#: Each of these escapes its parentheses on at least one engine.
ESCAPES = [
    # BigQuery / DuckDB: the `]` inside the string ended the old "identifier".
    "']' in unnest([']'])) or (true or 'x' in unnest(['['])",
    # DuckDB list literal, the same trick.
    "list_contains([']'], 'a')) or (true or list_contains(['['], 'b')",
    # PostgreSQL / Snowflake array subscripts holding a string.
    "arr[']'] = 1) or (true or arr['['] = 1",
    # SQL Server: an apostrophe inside a bracketed name is not a string there.
    "[a'b] ) or (1=1 or [c'd]",
    # A bracket that never closes cannot hide a parenthesis either way.
    "x = 1 [ ) or (true",
]

#: Ordinary predicates, in each engine's own spellings, that must pass.
ORDINARY = [
    "a[1] > 2",  # PostgreSQL / DuckDB subscript
    "arr[offset(0)] = 'x'",  # BigQuery
    "'a' in unnest(['a', 'b'])",  # BigQuery array literal
    "list_contains(['(', ')'], s)",  # parentheses inside strings inside brackets
    "[my col] = 1",  # SQL Server
    "[a]]b] = 1",  # SQL Server, an escaped `]`
    "s = '[' or s = ']'",
    "`my col` > 1",  # MySQL / BigQuery
    '"my col" > 1',  # PostgreSQL / Snowflake
    "name = 'it''s'",
]


@pytest.mark.parametrize("predicate", ESCAPES)
def test_shared_check_refuses_every_bracket_escape(predicate):
    """The shared check refuses every bracket escape."""
    assert row_filter_problem(predicate) is not None


@pytest.mark.parametrize("dialect", DIALECTS, ids=lambda d: d.__name__)
@pytest.mark.parametrize("predicate", ESCAPES)
def test_every_dialect_refuses_every_bracket_escape(dialect, predicate):
    """Every dialect's `check_where` refuses every bracket escape, naming side A."""
    with pytest.raises(ValueError, match=r"\[side A: .*\] row filter"):
        dialect(side="A").check_where(predicate)


@pytest.mark.parametrize("predicate", ORDINARY)
def test_ordinary_bracket_and_quote_spellings_pass(predicate):
    """Ordinary bracket and quote spellings pass the shared check."""
    assert row_filter_problem(predicate) is None


@pytest.mark.parametrize(
    "dialect",
    [d for d in DIALECTS if d is not SQLServerDialect],
    ids=lambda d: d.__name__,
)
def test_ordinary_spellings_pass_each_dialect(dialect):
    """SQL Server is left out: its own guard refuses a double quote."""
    for predicate in ORDINARY:
        dialect(side="A").check_where(predicate)


def test_the_bracket_reading_is_named_in_the_refusal():
    """The refusal names the SQL Server identifier reading of `[`."""
    problem = row_filter_problem("']' in unnest([']'])) or (true or 'x' in unnest(['['])")
    assert problem is not None and "SQL Server identifier" in problem
