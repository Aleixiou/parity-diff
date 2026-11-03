"""Each conformance finding, as the regression test that keeps it fixed.

The red-team convention: a confirmed bug was first a strict xfail asserting
the right behaviour; all six COV findings are fixed (2026-11-03), so the
markers are gone and each test is the smallest reproduction of what the
sweep found. `cov_known.FINDINGS` says what was wrong and how it was fixed.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from cov_engines import RUN

from parity.dialects.base import Dialect, get_dialect
from parity.reference import UNCHECKED, canonical_text, reference_token
from parity.types import LogicalType


def _duck(tmp_path, ddl: str, value: Any) -> tuple[Dialect, str]:
    """A read-only DuckDB dialect over `t(id, v <ddl>)` holding one value."""
    duckdb = pytest.importorskip("duckdb")
    path = str(tmp_path / "f.duckdb")
    con = duckdb.connect(path)
    con.execute(f"create table t (id bigint, v {ddl})")
    con.execute("insert into t values (1, ?)", [value])
    con.close()
    return get_dialect(f"duckdb:///{path}", side="B"), "main.t"


def _render(d: Dialect, table: str) -> tuple[str, Any]:
    """(normalize text, sample-path raw value) of column v, row 1."""
    (col,) = [c for c in d.columns(table) if c.name == "v"]
    (text,) = d.query(f"select {d.fetch_text(d.normalize(col))} from {d.qualify(table)}")[
        0
    ]
    (raw,) = d.query(f"select {d.sample_select(col)} from {d.qualify(table)}")[0]
    return d.fetched(text), d.sample_value(col, raw)


def _rules(d: Dialect, table: str, value: Any) -> Any:
    """The reference token of `value` read as column v's type."""
    (col,) = [c for c in d.columns(table) if c.name == "v"]
    return reference_token(col.logical_type, value, d.float_scale)


@pytest.mark.duckdb
@pytest.mark.parametrize("ddl", ["TIMESTAMP_S", "TIMESTAMP_MS"])
def test_cov1_duckdb_second_and_milli_timestamps_render_outside_the_ns_range(
    tmp_path, ddl
):
    """Year 1 fits TIMESTAMP_S/MS; rendering it must not go through nanoseconds."""
    d, t = _duck(tmp_path, ddl, "0001-01-01 00:00:00")
    try:
        text, _ = _render(d, t)
    finally:
        d.close()
    assert text == "0001-01-01 00:00:00.000000"


@pytest.mark.duckdb
def test_cov2_duckdb_timestamp_ns_renders_as_the_rules_say(tmp_path):
    """Against a microsecond engine a TIMESTAMP_NS is truncated, as SQL Server
    datetime2(7) and Snowflake TIMESTAMP(9) are; against another engine that
    holds nanoseconds (`sub_micro`) it keeps nine digits, as they do."""
    import dataclasses

    value = "2024-01-01 12:00:00.1234567"
    d, t = _duck(tmp_path, "TIMESTAMP_NS", value)
    try:
        text, _ = _render(d, t)
        (col,) = [c for c in d.columns(t) if c.name == "v"]
        fine = dataclasses.replace(col, sub_micro=True)
        (nine,) = d.query(f"select {d.normalize(fine)} from {d.qualify(t)}")[0]
        want = canonical_text(_rules(d, t, value))
        want_nine = canonical_text(
            reference_token(col.logical_type, value, d.float_scale, sub_micro=True)
        )
    finally:
        d.close()
    assert want == "2024-01-01 12:00:00.123456"
    assert text == want
    assert want_nine == "2024-01-01 12:00:00.123456700"
    assert nine == want_nine


@pytest.mark.duckdb
@pytest.mark.parametrize(
    ("ddl", "value"), [("DATE", "infinity"), ("TIMESTAMP", "-infinity")]
)
def test_cov3_duckdb_infinity_reaches_the_sample_check_as_infinity(tmp_path, ddl, value):
    """The sample check must see what `normalize` renders: `infinity`, not
    the largest date the driver can make."""
    d, t = _duck(tmp_path, ddl, value)
    try:
        text, raw = _render(d, t)
        got = _rules(d, t, raw)
    finally:
        d.close()
    assert text == value
    assert got is not UNCHECKED and canonical_text(got) == value


@pytest.mark.duckdb
def test_cov4_a_duckdb_bc_date_is_checked_by_the_sample(tmp_path):
    """44 BC is astronomical year -43, as `normalize` renders it."""
    d, t = _duck(tmp_path, "DATE", "0044-03-15 (BC)")
    try:
        text, raw = _render(d, t)
        got = _rules(d, t, raw)
    finally:
        d.close()
    assert text == "-43-03-15"
    assert got is not UNCHECKED and canonical_text(got) == text


@pytest.mark.duckdb
def test_cov5_the_duckdb_sample_reads_a_ns_timestamp_without_rounding(tmp_path):
    """The rules truncate; a raw value rounded into the next second is not
    the stored value."""
    d, t = _duck(tmp_path, "TIMESTAMP_NS", "1969-12-31 23:59:59.9999999")
    try:
        _, raw = _render(d, t)
        got = _rules(d, t, raw)
    finally:
        d.close()
    assert canonical_text(got) == "1969-12-31 23:59:59.999999"


@pytest.mark.snowflake
def test_cov6_snowflake_time_keeps_its_fractional_seconds(snowflake_url):
    """12:34:56.5 and 12:34:56 are different values; they must render apart
    (and 12:34:56.5 as PostgreSQL, DuckDB and BigQuery print it)."""
    w = get_dialect(snowflake_url, side="W")
    table = f"COV_TIME_{RUN}_{os.getpid()}"
    cur = w._conn.cursor()
    try:
        cur.execute(f'create or replace table "{table}" ("id" number(38,0), "v" time(9))')
        cur.execute(
            f'insert into "{table}" values (?, ?), (?, ?)',
            [1, ("TEXT", "12:34:56.5"), 2, ("TEXT", "12:34:56")],
            _force_qmark_paramstyle=True,
        )
        d = get_dialect(snowflake_url, side="A")
        try:
            (col,) = [c for c in d.columns(table) if c.name == "v"]
            assert col.logical_type is LogicalType.UNKNOWN
            rows = dict(
                d.query(
                    f'select "id", {d.normalize(col)} from {d.qualify(table)} order by 1'
                )
            )
        finally:
            d.close()
    finally:
        cur.execute(f'drop table if exists "{table}"')
        cur.close()
        w.close()
    assert rows[1] != rows[2], rows
    assert rows[1] == "12:34:56.5", rows
