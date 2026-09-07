"""Offline unit tests for the BigQuery dialect's SQL rendering.

BigQuery is a DRAFT (see src/parity/dialects/bigquery_dialect.py): it cannot be
called supported until a live encoding run agrees with another engine, and it
has no free CI instance, so the live proof is a manual step. These tests pin the
*SQL the dialect generates* - every GoogleSQL rendering decision from the
docstring, in pure string form - so the logic stays covered on every push. Only
`connect`/`close`/`query`, which touch the client library, are left to a live
run (they carry `# pragma: no cover`); `columns()` is exercised here through a
stubbed `query`.

None of this proves BigQuery *works* - only a live run does that. It proves the
dialect renders the SQL its author intended, so a live failure points at a wrong
assumption about GoogleSQL rather than a typo.
"""

from __future__ import annotations

from parity.dialects.bigquery_dialect import BigQueryDialect, map_type_bigquery
from parity.types import Column, LogicalType


def _d() -> BigQueryDialect:
    """A dialect instance; no client is opened."""
    d = BigQueryDialect(side="A")
    d.project = "my-proj"
    d.default_schema = "ds"
    return d


def _col(name: str, t: LogicalType, raw: str = "") -> Column:
    """A column of the given logical (and optional raw) type, for rendering."""
    return Column(name, t, raw or t.value)


def test_hash_expr_casts_the_hex_prefix_to_int64():
    """No bit-cast, no CONV: TO_HEX(MD5(x)) then a 0x-prefixed INT64 cast."""
    assert _d().hash_expr("'abc'") == (
        "cast(concat('0x', substr(to_hex(md5('abc')), 1, 15)) as int64)"
    )


def test_separator_and_sentinel_use_chr():
    r"""BigQuery has CHR; the sentinel is CHR(92)||'N' because `\N` is not a
    valid GoogleSQL escape and a literal would raise."""
    d = _d()
    assert d.separator_sql() == "chr(31)"
    assert d.null_sentinel_sql() == "concat(chr(92), 'N')"


def test_concat_uses_array_to_string_not_concat_ws():
    """GoogleSQL has no concat_ws; a flat ARRAY_TO_STRING of coalesced parts is
    byte-identical to the other engines' concat_ws output."""
    assert _d()._concat(["x", "y", "z"]) == "array_to_string([x, y, z], chr(31))"


def test_identifiers_are_back_quoted_and_cannot_break_out():
    """Names are back-quoted; a stray backtick is stripped, not escaped."""
    d = _d()
    assert d.quote("id") == "`id`"
    assert d.quote("a`b") == "`ab`"
    assert d.qualify("t") == "`my-proj`.`ds`.`t`"
    assert d.qualify("other.tbl") == "`my-proj`.`other`.`tbl`"


def test_integer_division_and_widening():
    """DIV truncates toward zero; NUMERIC/BIGNUMERIC hold the widened sums."""
    d = _d()
    assert d.int_div("a", "b") == "div(a, b)"
    assert d.wide_int("k") == "cast(k as numeric)"
    assert d.sum_wide("h") == "ifnull(sum(cast(h as bignumeric)), 0)"


def test_normalize_renders_each_logical_type():
    """Every branch of the canonical-text encoding, null-safe."""
    d = _d()
    sentinel = "concat(chr(92), 'N')"
    assert d.normalize(_col("c", LogicalType.INTEGER)) == \
        f"coalesce(cast(`c` as string), {sentinel})"
    assert d.normalize(_col("c", LogicalType.DECIMAL)) == \
        f"coalesce(format('%.6f', `c`), {sentinel})"
    assert d.normalize(_col("c", LogicalType.BOOLEAN)) == \
        f"coalesce(case when `c` then 'true' when not `c` then 'false' end, {sentinel})"
    assert d.normalize(_col("c", LogicalType.DATE)) == \
        f"coalesce(format_date('%Y-%m-%d', `c`), {sentinel})"
    assert d.normalize(_col("c", LogicalType.STRING)) == \
        f"coalesce(cast(`c` as string), {sentinel})"


def test_float_special_cases_non_finite_values():
    """Inf/-Inf/NaN become the fixed tokens; finite floats are formatted."""
    r = _d().normalize(_col("f", LogicalType.FLOAT))
    assert "is_inf(`f`)" in r and "'Infinity'" in r and "'-Infinity'" in r
    assert "is_nan(`f`)" in r and "'NaN'" in r
    assert "format('%.6f', `f`)" in r


def test_timestamp_and_datetime_use_different_format_functions():
    """TIMESTAMP is an instant (pin UTC); DATETIME is naive (no zone)."""
    d = _d()
    ts = d.normalize(_col("t", LogicalType.TIMESTAMP, "TIMESTAMP"))
    dt = d.normalize(_col("t", LogicalType.TIMESTAMP, "DATETIME"))
    assert "format_timestamp('%Y-%m-%d %H:%M:%E6S', `t`, 'UTC')" in ts
    assert "format_datetime('%Y-%m-%d %H:%M:%E6S', `t`)" in dt


def test_float_scale_flows_into_the_format_string():
    """--float-scale changes the padded precision on both decimal and float."""
    d = BigQueryDialect(side="A", float_scale=2)
    assert "format('%.2f', `c`)" in d.normalize(_col("c", LogicalType.DECIMAL))


def test_map_type_keeps_int64_and_numeric_distinct():
    """Unlike Snowflake's NUMBER, BigQuery separates INT64 from NUMERIC, so no
    scale lookup is needed."""
    assert map_type_bigquery("INT64") is LogicalType.INTEGER
    assert map_type_bigquery("NUMERIC") is LogicalType.DECIMAL
    assert map_type_bigquery("BIGNUMERIC") is LogicalType.DECIMAL
    assert map_type_bigquery("FLOAT64") is LogicalType.FLOAT
    assert map_type_bigquery("BOOL") is LogicalType.BOOLEAN
    assert map_type_bigquery("DATE") is LogicalType.DATE
    assert map_type_bigquery("TIMESTAMP") is LogicalType.TIMESTAMP
    assert map_type_bigquery("DATETIME") is LogicalType.TIMESTAMP
    assert map_type_bigquery("STRING") is LogicalType.STRING
    assert map_type_bigquery("GEOGRAPHY") is LogicalType.UNKNOWN
    assert map_type_bigquery("NUMERIC(38, 9)") is LogicalType.DECIMAL


def test_columns_reads_information_schema():
    """`columns()` builds a dataset-scoped INFORMATION_SCHEMA query and types
    each row - checked here through a stubbed query, no client."""

    class Stubbed(BigQueryDialect):
        def query(self, sql):  # type: ignore[override]
            """Return canned INFORMATION_SCHEMA rows instead of a live query."""
            assert "INFORMATION_SCHEMA.COLUMNS" in sql
            assert "`my-proj`" in sql and "`ds`" in sql
            return [("id", "INT64"), ("amount", "NUMERIC"), ("name", "STRING")]

    d = Stubbed(side="A")
    d.project = "my-proj"
    d.default_schema = "ds"
    cols = d.columns("t")
    assert [(c.name, c.logical_type) for c in cols] == [
        ("id", LogicalType.INTEGER),
        ("amount", LogicalType.DECIMAL),
        ("name", LogicalType.STRING),
    ]
