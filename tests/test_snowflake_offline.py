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

import pytest

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
    """Division of an exact multiple, which NUMBER's 6-place quotient cannot
    round (RT-B7); NUMBER(38,0) cannot overflow."""
    d = _d()
    assert d.int_div("x", "y") == "cast(((x) - mod((x), (y))) / (y) as number(38,0))"
    assert d.wide_int("k") == "cast((k) as number(38,0))"
    assert d.sum_wide("h") == "coalesce(sum(cast((h) as number(38,0))), 0)"


_ESCAPED = (
    'replace(replace(cast("c" as varchar), chr(92), (chr(92) || chr(92))), '
    "chr(31), (chr(92) || 's'))"
)


def test_normalize_renders_each_logical_type():
    """Every branch of the canonical-text encoding, null-safe."""
    d = _d()
    sentinel = "(chr(92) || 'N')"
    cases = {
        LogicalType.INTEGER: 'cast("c" as varchar)',
        # ROUND, never a cast to number(38, 6), which held 32 integer digits
        # (RT-HF8); the places ROUND keeps short of six are padded with zeros.
        LogicalType.DECIMAL: (
            "case when contains(cast(round(\"c\", 6) as varchar), '.') "
            "then cast(round(\"c\", 6) as varchar) || repeat('0', 6 - "
            "(length(cast(round(\"c\", 6) as varchar)) - position('.', "
            'cast(round("c", 6) as varchar)))) '
            "else cast(round(\"c\", 6) as varchar) || '.000000' end"
        ),
        LogicalType.BOOLEAN: "case when \"c\" then 'true' when not \"c\" then 'false' end",
        LogicalType.DATE: "to_char(\"c\", 'YYYY-MM-DD')",
        LogicalType.TIMESTAMP: "to_char(\"c\", 'YYYY-MM-DD HH24:MI:SS.FF6')",
        # Arbitrary text is escaped: backslash doubled, chr(31) as \s.
        LogicalType.STRING: _ESCAPED,
        LogicalType.UNKNOWN: _ESCAPED,
    }
    for t, inner in cases.items():
        assert d.normalize(_col("c", t)) == f"coalesce({inner}, {sentinel})", t


def test_float_builds_the_shortest_repr_not_a_plain_cast():
    """FLOAT is the canonical float form (verified live in test_float_canonical
    and test_snowflake): the 15/16/17-digit candidates rounded half to even,
    tokens for Infinity and NaN, and a refusal past 2^116 naming the column.
    A plain `cast(c as number(38, 6))` rounded the exact binary value and
    disagreed with every other engine on 215 of the 1,565 sample doubles."""
    sql = _d().normalize(_col("ratio", LogicalType.FLOAT))
    assert sql.startswith("coalesce(case when \"ratio\" = 'inf'::float then 'Infinity' ")
    # Per band: the 15- and 16-digit candidates are tested and then returned,
    # the 17-digit one only returned - five roundings, two bands.
    assert sql.count("'HALF_TO_EVEN'") == 10
    assert "when \"ratio\" = 'nan'::float then 'NaN'" in sql
    assert "in column ''ratio''" in sql
    with pytest.raises(ValueError, match="cannot be compared at float_scale 21"):
        SnowflakeDialect(float_scale=21).normalize(_col("ratio", LogicalType.FLOAT))


def test_map_type_splits_number_by_scale_and_maps_the_rest():
    """Snowflake reports integer and decimal both as NUMBER; only scale tells
    them apart, and a wrong split would render an integer key as `42.000000`."""
    assert map_type_snowflake("NUMBER", 0) is LogicalType.INTEGER
    assert map_type_snowflake("NUMBER(38,0)", 0) is LogicalType.INTEGER
    assert map_type_snowflake("NUMBER", 2) is LogicalType.DECIMAL
    assert (
        map_type_snowflake("NUMBER", None) is LogicalType.DECIMAL
    )  # no scale -> not integer
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
    assert map_type_snowflake("VARIANT", None) is LogicalType.JSON
    # A non-integer scale value is treated defensively as decimal, not a crash.
    assert map_type_snowflake("NUMBER", "oops") is LogicalType.DECIMAL


def test_columns_reads_numeric_scale_to_split_number():
    """`columns()` turns information_schema rows into typed Columns, splitting
    NUMBER on its scale - checked here through a stubbed query, no connection."""

    class Stubbed(SnowflakeDialect):
        def query(self, sql):  # type: ignore[override]
            """Return canned information_schema rows instead of hitting a DB."""
            if "information_schema.columns" not in sql:
                return []  # the policy lookups (RT-HF4/5): nothing attached
            assert "PARITY_TEST" in sql or "ENC" in sql or "ORDERS" in sql
            return [
                ("ID", "NUMBER", 0, None),
                ("AMOUNT", "NUMBER", 2, None),
                ("STATUS", "TEXT", None, None),
                ("CREATED_AT", "TIMESTAMP_NTZ", None, 9),
                ("SEEN_AT", "TIMESTAMP_TZ", None, 6),
                ("LOCAL_AT", "TIMESTAMP_LTZ", None, 3),
            ]

    cols = Stubbed(side="A").columns("ENC.ORDERS")
    assert [(c.name, c.logical_type) for c in cols] == [
        ("ID", LogicalType.INTEGER),
        ("AMOUNT", LogicalType.DECIMAL),
        ("STATUS", LogicalType.STRING),
        ("CREATED_AT", LogicalType.TIMESTAMP),
        ("SEEN_AT", LogicalType.TIMESTAMP),
        ("LOCAL_AT", LogicalType.TIMESTAMP),
    ]
    # RT-C6: TZ and LTZ are instants, NTZ is naive - none says "with time zone".
    assert [c.tz_aware for c in cols[3:]] == [False, True, True]
    # The precision rides in raw_type, so only the 9-digit column is caveated.
    assert [c.raw_type for c in cols[3:]] == [
        "TIMESTAMP_NTZ(9)",
        "TIMESTAMP_TZ(6)",
        "TIMESTAMP_LTZ(3)",
    ]
    notes = Stubbed(side="A").caveats(cols)
    assert any("CREATED_AT" in n and "microsecond" in n for n in notes), notes
    assert not any("SEEN_AT" in n or "LOCAL_AT" in n for n in notes), notes


def test_every_run_says_it_is_not_one_snapshot():
    """RT-D7: READ COMMITTED only, said in the output and not just the docs."""
    (note,) = _d().caveats([])
    assert "READ COMMITTED" in note


def test_timestamp_tz_is_moved_to_utc_and_ntz_is_not():
    """RT-C11: TO_CHAR prints a TIMESTAMP_TZ in its stored offset; only TZ is
    converted (NTZ is naive, LTZ already prints in the pinned UTC session)."""
    d = _d()
    fmt = "'YYYY-MM-DD HH24:MI:SS.FF6'"
    tz = Column("T", LogicalType.TIMESTAMP, "TIMESTAMP_TZ(9)")
    assert f"to_char(convert_timezone('UTC', \"T\"), {fmt})" in d.normalize(tz)
    for raw in ("TIMESTAMP_NTZ(9)", "TIMESTAMP_LTZ(9)"):
        col = Column("T", LogicalType.TIMESTAMP, raw)
        assert "convert_timezone" not in d.normalize(col)


def test_variant_keeps_its_json_type():
    """RT-C15: TO_JSON keeps the quotes, so the number 1 and the string "1"
    differ; a type JSON cannot name (DATE, BINARY, ...) keeps its TYPEOF."""
    d = _d()
    for raw in ("VARIANT", "OBJECT", "ARRAY"):
        col = Column("V", map_type_snowflake(raw, None), raw)
        sql = d.normalize(col)
        assert 'to_json("V")' in sql
        assert 'when "V" is not null then typeof("V") || \':\' ||' in sql


def test_a_row_access_policy_on_the_table_is_a_caveat_naming_it():
    """RT-HF5 offline: a row access policy hides rows from both sides alike,
    so the rows it hides differ unseen. The live test needs an account; this
    stub answers POLICY_REFERENCES with one policy, planted on the table, and
    the caveat must name it - and a table with none must draw no such caveat."""

    policies: list[tuple[str, str, str | None]] = []

    class Stubbed(SnowflakeDialect):
        def query(self, sql):  # type: ignore[override]
            """Canned catalog answers: one table, its columns, its policies."""
            if "current_database()" in sql:
                return [("DB",)]
            if "information_schema.tables" in sql:
                return [("BASE TABLE",)]
            if "policy_references" in sql:
                return policies
            return [("ID", "NUMBER", 0, None), ("V", "TEXT", None, None)]

    plain = Stubbed(side="A")
    cols = plain.columns("S.T")
    assert not any("row access policy" in n for n in plain.caveats(cols))

    policies.append(("ROW_ACCESS_POLICY", "TENANT_RAP", None))
    filtered = Stubbed(side="A")
    cols = filtered.columns("S.T")
    notes = [n for n in filtered.caveats(cols) if "row access policy" in n]
    assert len(notes) == 1 and "TENANT_RAP" in notes[0] and "S.T" in notes[0], notes
