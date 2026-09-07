"""BigQuery against DuckDB, end to end - the verification the draft needs.

DRAFT dialect (see src/parity/dialects/bigquery_dialect.py). BigQuery earns the
word "supported" only once this file passes against a real project, byte for
byte with DuckDB. It is the BigQuery twin of test_snowflake.py, and the only
BigQuery-specific content is the fixture DDL (GoogleSQL) and the decisions the
draft made: the row hash goes through a 0x-hex INT64 cast, canonical text is
joined with ARRAY_TO_STRING (no concat_ws), decimals/floats are padded with
FORMAT('%.6f', ...), and the NULL sentinel is CHR(92)||'N'. Each is verified
below by the values agreeing with DuckDB, not by reading the SQL.

Skips cleanly unless PARITY_TEST_BIGQUERY names a reachable project, so it never
runs in CI. Run it with:

    pip install -e ".[duckdb,bigquery]" pytest
    # auth: GOOGLE_APPLICATION_CREDENTIALS=/path/key.json, or gcloud auth
    #       application-default login
    set PARITY_TEST_BIGQUERY=bigquery://your-project/parity_test
    pytest tests/test_bigquery.py -v
"""

from __future__ import annotations

import pytest
from conftest import duckdb_write, open_bigquery, open_duckdb

from parity.engine import diff

pytestmark = pytest.mark.bigquery

N = 5_000

#: Side B: the DuckDB reference, same shape as the Snowflake twin - a real
#: boolean, a decimal, a naive timestamp - so any reported difference is a real
#: one, not a type mismatch.
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

#: Side A: the BigQuery table, GoogleSQL. `id` is INT64 (bisected directly);
#: `amount` is NUMERIC divided by 100 to stay exact; `is_refunded` is BOOL;
#: `created_at` is a UTC TIMESTAMP built with TIMESTAMP_ADD.
BIGQUERY_SELECT = f"""
select id,
       mod(id, 97)                                            as customer_id,
       cast(mod(id * 7, 100000) as numeric) / 100             as amount,
       case when mod(id, 3) = 0 then 'paid'
            when mod(id, 3) = 1 then 'open' else 'void' end   as status,
       (mod(id, 11) = 0)                                      as is_refunded,
       timestamp_add(timestamp '2024-01-01 00:00:00',
                     interval mod(id, 86400) second)          as created_at,
       case when mod(id, 13) = 0 then null
            else concat('note ', cast(id as string)) end      as note
from unnest(generate_array(1, {N})) as id
"""


@pytest.fixture(scope="module")
def duck_path(tmp_path_factory) -> str:
    """Side B: the DuckDB reference table, built once and opened read-only."""
    path = str(tmp_path_factory.mktemp("bigquery_it") / "b.duckdb")
    con = duckdb_write(path)
    try:
        con.execute(DUCKDB_TABLE)
    finally:
        con.close()
    return path


def _table(dialect) -> str:
    """The fully-qualified BigQuery table name for the connected project/dataset."""
    return f"{dialect.default_schema}.orders"


def _build_bigquery(bigquery_url: str, plant: str | None) -> None:
    """Create the BigQuery `orders` table, optionally with one planted defect.

    Runs DDL through the client. The parity dialect itself issues SELECT only;
    this is the sole place the tests write to BigQuery.
    """
    a = open_bigquery(bigquery_url, side="A")
    qualified = a.qualify("orders")
    client = a._client
    try:
        client.query(f"create or replace table {qualified} as {BIGQUERY_SELECT}").result()
        if plant == "changed":
            client.query(
                f"update {qualified} set amount = amount + 0.01 where id = 1234"
            ).result()
        elif plant == "deleted":
            client.query(f"delete from {qualified} where id = 777").result()
        elif plant == "null_trap":
            client.query(
                f"update {qualified} set note = '' where id = 13"
            ).result()
    finally:
        a.close()


def _diff(bigquery_url: str, duck_path: str, **kwargs):
    """Diff the BigQuery orders table against the DuckDB one."""
    a = open_bigquery(bigquery_url, side="A")
    b = open_duckdb(duck_path, side="B")
    try:
        return diff(a, b, _table(a), "main.orders", "id", **kwargs)
    finally:
        a.close()
        b.close()


def test_the_hash_constant_agrees_with_the_other_engines(bigquery_url):
    """The whole cross-engine contract in one number.

    BigQuery reaches it through a 0x-hex INT64 cast, a fifth distinct path after
    PostgreSQL's bit-cast, DuckDB's hex-cast, MySQL's CONV and Snowflake's
    MD5_NUMBER_UPPER64. If this disagrees, nothing else can be trusted.
    """
    a = open_bigquery(bigquery_url, side="A")
    try:
        got = a.query(f"select {a.hash_expr(chr(39) + 'abc' + chr(39))}")[0][0]
        assert int(got) == 648541476951500027
    finally:
        a.close()


def test_identical_tables_match_and_download_nothing(bigquery_url, duck_path):
    """The headline claim, cross-engine: agreement moves zero rows."""
    _build_bigquery(bigquery_url, plant=None)
    result = _diff(bigquery_url, duck_path)
    assert result.identical, [(d.key, d.columns) for d in result.diffs[:5]]
    assert result.stats.rows_downloaded == 0


def test_a_changed_decimal_is_found_on_exactly_that_row(bigquery_url, duck_path):
    """A one-cent change on one row is reported as that row, that column."""
    _build_bigquery(bigquery_url, plant="changed")
    result = _diff(bigquery_url, duck_path)
    assert [(d.key, d.kind) for d in result.diffs] == [(1234, "different")]
    assert result.diffs[0].columns == ["amount"]


def test_a_deleted_row_is_reported_only_in_b(bigquery_url, duck_path):
    """A row missing from BigQuery is only_in_b, not an error."""
    _build_bigquery(bigquery_url, plant="deleted")
    result = _diff(bigquery_url, duck_path)
    assert [(d.key, d.kind) for d in result.diffs] == [(777, "only_in_b")]


def test_null_versus_empty_string_is_caught(bigquery_url, duck_path):
    """The trap naive tools miss: NULL on one side, '' on the other."""
    _build_bigquery(bigquery_url, plant="null_trap")
    result = _diff(bigquery_url, duck_path)
    assert [(d.key, d.kind) for d in result.diffs] == [(13, "different")]
    assert result.diffs[0].columns == ["note"]
