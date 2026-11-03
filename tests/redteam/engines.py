"""Throwaway-table builders for the red-team value tests, one per engine.

Each engine builds a table from engine-native column DDL plus rows, then opens
a read-only parity dialect over it. Tables are prefixed `rt_val_` so concurrent
red-team runs on the shared servers never collide, and every builder drops what
it created. Rows go through driver parameter binding where the value can be
bound; values only expressible as SQL literals (BC dates, `infinity`) use raw
insert statements with a `{t}` placeholder for the table name.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse, urlunparse

import pytest
from conftest import BIGQUERY_URL, MSSQL_URL, MYSQL_URL, PG_URL, mysql_rw_cursor

from parity.dialects.base import Dialect, get_dialect

PREFIX = "rt_val_"
#: SQL Server database of our own (UTF-8, snapshot allowed), so nothing here
#: depends on another suite's fixture databases.
MSSQL_DB = "rt_val_db"
MSSQL_COLLATION = "Latin1_General_100_CI_AS_SC_UTF8"


class Engine:
    """One engine: build a table, open a dialect over it, drop it."""

    name: str
    placeholder = "%s"

    def build(
        self,
        table: str,
        ddl: str,
        rows: Sequence[Sequence[Any]] = (),
        raw: Sequence[str] = (),
    ) -> str:
        """Create `table` with column DDL `ddl`, insert `rows` and `raw`."""
        raise NotImplementedError

    def open(self, side: str) -> Dialect:
        """A read-only parity dialect for this engine."""
        raise NotImplementedError

    def ref(self, table: str) -> str:
        """The name parity is given for `table`."""
        return table

    def drop_all(self) -> None:
        """Drop every table this engine object created."""


class DuckDBEngine(Engine):
    """One DuckDB file per side, so the two sides never share a writer."""

    name = "duckdb"
    placeholder = "?"

    def __init__(self, directory: str) -> None:
        self.directory = directory
        self.path = ""

    def build(self, table, ddl, rows=(), raw=()):
        import duckdb

        self.path = os.path.join(self.directory, f"{table}.duckdb")
        if os.path.exists(self.path):
            os.remove(self.path)
        con = duckdb.connect(self.path)
        try:
            con.execute(f'create table "{table}" ({ddl})')
            for r in rows:
                marks = ", ".join("?" * len(r))
                con.execute(f'insert into "{table}" values ({marks})', list(r))
            for stmt in raw:
                con.execute(stmt.format(t=f'"{table}"'))
        finally:
            con.close()
        return table

    def open(self, side):
        return get_dialect(f"duckdb:///{self.path}", side=side)


class PostgresEngine(Engine):
    name = "postgres"

    def __init__(self) -> None:
        import psycopg

        try:
            self.con = psycopg.connect(PG_URL, autocommit=True, connect_timeout=5)
        except Exception as exc:
            pytest.skip(f"no PostgreSQL: {type(exc).__name__}")
        self.tables: list[str] = []

    def build(self, table, ddl, rows=(), raw=()):
        q = f'public."{table}"'
        self.con.execute(f"drop table if exists {q}")
        self.con.execute(f"create table {q} ({ddl})")
        self.tables.append(q)
        with self.con.cursor() as cur:
            for r in rows:
                cur.execute(f"insert into {q} values ({', '.join(['%s'] * len(r))})", r)
            for stmt in raw:
                cur.execute(stmt.format(t=q))
        return table

    def ref(self, table):
        return f"public.{table}"

    def open(self, side):
        return get_dialect(PG_URL, side=side)

    def drop_all(self):
        for q in self.tables:
            self.con.execute(f"drop table if exists {q}")
        self.con.close()


class MySQLEngine(Engine):
    name = "mysql"

    def __init__(self) -> None:
        try:
            self.d = get_dialect(MYSQL_URL, side="W")
        except Exception as exc:
            pytest.skip(f"no MySQL: {type(exc).__name__}")
        # The dialect's session is READ ONLY; this one only builds fixtures.
        mysql_rw_cursor(self.d).close()
        self.tables: list[str] = []

    def _run(self, sql, params=None):
        cur = self.d._conn.cursor()
        cur.execute(sql, params)
        cur.close()
        self.d._conn.commit()

    def build(self, table, ddl, rows=(), raw=()):
        self._run(f"drop table if exists `{table}`")
        self._run(f"create table `{table}` ({ddl}) default charset=utf8mb4")
        self.tables.append(table)
        for r in rows:
            self._run(f"insert into `{table}` values ({', '.join(['%s'] * len(r))})", r)
        for stmt in raw:
            self._run(stmt.format(t=f"`{table}`"))
        return table

    def open(self, side):
        return get_dialect(MYSQL_URL, side=side)

    def drop_all(self):
        for t in self.tables:
            self._run(f"drop table if exists `{t}`")
        self.d.close()


class BigQueryEngine(Engine):
    """BigQuery is billed: tiny tables only, literal inserts only (`raw`)."""

    name = "bigquery"

    def __init__(self) -> None:
        if not BIGQUERY_URL:
            pytest.skip("PARITY_TEST_BIGQUERY not set")
        from google.cloud import bigquery

        from parity.dialects.bigquery_dialect import parse_bigquery_url

        self.project, self.dataset, location = parse_bigquery_url(BIGQUERY_URL)
        try:
            self.client = bigquery.Client(project=self.project, location=location)
        except Exception as exc:
            pytest.skip(f"no BigQuery: {type(exc).__name__}")
        self.tables: list[str] = []

    def build(self, table, ddl, rows=(), raw=()):
        if rows:
            raise ValueError("BigQueryEngine takes literal `raw` inserts only")
        q = f"`{self.project}`.`{self.dataset}`.`{table}`"
        self.client.query_and_wait(f"create or replace table {q} ({ddl})")
        self.tables.append(table)
        for stmt in raw:
            self.client.query_and_wait(stmt.format(t=q))
        return table

    def ref(self, table):
        return table

    def open(self, side):
        return get_dialect(BIGQUERY_URL, side=side)

    def drop_all(self):
        for t in self.tables:
            self.client.delete_table(
                f"{self.project}.{self.dataset}.{t}", not_found_ok=True
            )
        self.client.close()


def _mssql_db_url() -> str:
    u = urlparse(MSSQL_URL)
    return urlunparse(u._replace(path="/" + MSSQL_DB))


class MSSQLEngine(Engine):
    name = "mssql"
    placeholder = "?"

    def __init__(self) -> None:
        if not MSSQL_URL:
            pytest.skip("PARITY_TEST_MSSQL not set")
        import mssql_python

        from parity.dialects.mssql_dialect import parse_mssql_url

        try:
            odbc, _ = parse_mssql_url(MSSQL_URL)
            master = mssql_python.connect(odbc, autocommit=True)
        except Exception as exc:
            pytest.skip(f"no SQL Server: {type(exc).__name__}")
        cur = master.cursor()
        cur.execute(f"select 1 from sys.databases where name = '{MSSQL_DB}'")
        if not cur.fetchall():
            cur.execute(f"create database [{MSSQL_DB}] collate {MSSQL_COLLATION}")
            cur.execute(f"alter database [{MSSQL_DB}] set allow_snapshot_isolation on")
        cur.close()
        master.close()
        odbc, _ = parse_mssql_url(_mssql_db_url())
        self.con = mssql_python.connect(odbc, autocommit=True)
        self.tables: list[str] = []

    def _run(self, sql, params=None):
        cur = self.con.cursor()
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(sql, params)
        cur.close()

    def build(self, table, ddl, rows=(), raw=()):
        q = f"dbo.[{table}]"
        self._run(f"drop table if exists {q}")
        self._run(f"create table {q} ({ddl})")
        self.tables.append(q)
        for r in rows:
            self._run(f"insert into {q} values ({', '.join(['?'] * len(r))})", list(r))
        for stmt in raw:
            self._run(stmt.format(t=q))
        return table

    def ref(self, table):
        return f"dbo.{table}"

    def open(self, side):
        return get_dialect(_mssql_db_url(), side=side)

    def drop_all(self):
        for q in self.tables:
            self._run(f"drop table if exists {q}")
        self.con.close()
