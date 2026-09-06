"""Offline unit tests for the Snowflake dialect's SQL rendering.

The live end-to-end proof is `test_snowflake.py`, which needs a real account and
so cannot run in CI. These tests pin the *SQL the dialect generates* - every
rendering decision from CLAUDE.md section 4, in pure string form - so the logic
stays covered on every push without a database. Only `connect`/`close`/`query`,
which touch the live driver, are left to the credential-gated live test (they
carry `# pragma: no cover`); `columns()` is exercised here through a stubbed
`query`.
"""

from __future__ import annotations

from parity.dialects.snowflake_dialect import SnowflakeDialect, map_type_snowflake
from parity.types import Column, LogicalType


def _d() -> SnowflakeDialect:
    """A dialect instance; no connection is opened."""
    return SnowflakeDialect(side="A")


def _col(name: str, t: LogicalType) -> Column:
    """A column of the given logical type, for rendering."""
    return Column(name, t, name)


def test_hash_expr_is_the_top_60_bits_of_the_md5_number():
    """The whole cross-engine contract: 15 hex chars = top 64 bits, drop 4."""
    assert _d().hash_expr("'abc'") == "floor(md5_number_upper64('abc') / 16)"


def test_the_null_sentinel_is_built_from_chr_92():
    r"""A literal '\N' is unsafe on Snowflake (it processes backslash escapes)."""
    assert _d().null_sentinel_sql() == "(chr(92) || 'N')"


def test_quote_doubles_embedded_quotes():
    """The injection boundary - names arrive from the command line."""
    assert _d().quote('a"b') == '"a""b"'


def test_integer_division_and_widening():
    """`/` is exact on NUMBER and `floor` truncates; NUMBER(38,0) cannot overflow."""
    d = _d()
    assert d.int_div("x", "y") == "floor((x) / (y))"
    assert d.wide_int("k") == "cast((k) as number(38,0))"
    assert d.sum_wide("h") == "coalesce(sum(cast((h) as number(38,0))), 0)"


def test_normalize_renders_each_logical_type():
    """Every branch of the canonical-text encoding, null-safe."""
    d = _d()
    sentinel = "(chr(92) || 'N')"
    cases = {
        LogicalType.INTEGER: 'cast("c" as varchar)',
        LogicalType.DECIMAL: 'cast(cast("c" as number(38,6)) as varchar)',
        LogicalType.FLOAT: 'cast(cast("c" as number(38,6)) as varchar)',
        LogicalType.BOOLEAN: 'case when "c" then \'true\' when not "c" then \'false\' end',
        LogicalType.DATE: 'to_char("c", \'YYYY-MM-DD\')',
        LogicalType.TIMESTAMP: 'to_char("c", \'YYYY-MM-DD HH24:MI:SS.FF6\')',
        LogicalType.STRING: 'cast("c" as varchar)',
        LogicalType.UNKNOWN: 'cast("c" as varchar)',
    }
    for t, inner in cases.items():
        assert d.normalize(_col("c", t)) == f"coalesce({inner}, {sentinel})", t


def test_map_type_splits_number_by_scale_and_maps_the_rest():
    """Snowflake reports integer and decimal both as NUMBER; only scale tells
    them apart, and a wrong split would render an integer key as `42.000000`."""
    assert map_type_snowflake("NUMBER", 0) is LogicalType.INTEGER
    assert map_type_snowflake("NUMBER(38,0)", 0) is LogicalType.INTEGER
    assert map_type_snowflake("NUMBER", 2) is LogicalType.DECIMAL
    assert map_type_snowflake("NUMBER", None) is LogicalType.DECIMAL  # no scale -> not integer
    assert map_type_snowflake("DECIMAL", 6) is LogicalType.DECIMAL
    assert map_type_snowflake("INT", 0) is LogicalType.INTEGER
    assert map_type_snowflake("FLOAT", None) is LogicalType.FLOAT
    assert map_type_snowflake("DOUBLE", None) is LogicalType.FLOAT
    assert map_type_snowflake("BOOLEAN", None) is LogicalType.BOOLEAN
    assert map_type_snowflake("DATE", None) is LogicalType.DATE
    assert map_type_snowflake("TIMESTAMP_NTZ", None) is LogicalType.TIMESTAMP
    assert map_type_snowflake("DATETIME", None) is LogicalType.TIMESTAMP
    assert map_type_snowflake("TEXT", None) is LogicalType.STRING
    assert map_type_snowflake("VARCHAR", None) is LogicalType.STRING
    assert map_type_snowflake("VARIANT", None) is LogicalType.UNKNOWN
    # A non-integer scale value is treated defensively as decimal, not a crash.
    assert map_type_snowflake("NUMBER", "oops") is LogicalType.DECIMAL


def test_columns_reads_numeric_scale_to_split_number():
    """`columns()` turns information_schema rows into typed Columns, splitting
    NUMBER on its scale - checked here through a stubbed query, no connection."""

    class Stubbed(SnowflakeDialect):
        def query(self, sql):  # type: ignore[override]
            """Return canned information_schema rows instead of hitting a DB."""
            assert "information_schema.columns" in sql
            assert "PARITY_TEST" in sql or "ENC" in sql or "ORDERS" in sql
            return [
                ("ID", "NUMBER", 0),
                ("AMOUNT", "NUMBER", 2),
                ("STATUS", "TEXT", None),
                ("CREATED_AT", "TIMESTAMP_NTZ", None),
            ]

    cols = Stubbed(side="A").columns("ENC.ORDERS")
    assert [(c.name, c.logical_type) for c in cols] == [
        ("ID", LogicalType.INTEGER),
        ("AMOUNT", LogicalType.DECIMAL),
        ("STATUS", LogicalType.STRING),
        ("CREATED_AT", LogicalType.TIMESTAMP),
    ]
