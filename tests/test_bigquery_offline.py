"""Offline unit tests for the BigQuery dialect's SQL rendering.

The live proof is `test_bigquery.py`, which needs a real project and so never
runs in CI. These pin the *SQL the dialect generates* and its parsing and
metadata logic through a stubbed client, so every decision stays covered on
every push. Only `connect` and `close`, which need real credentials, are left
to the live test. Each rendering assertion here records a value the live run
verified against DuckDB; the reason is in the dialect's comments.
"""

from __future__ import annotations

import pytest

from parity.dialects.base import get_dialect
from parity.dialects.bigquery_dialect import (
    BigQueryDialect,
    map_type_bigquery,
    parse_bigquery_url,
)
from parity.engine import _is_tz_aware
from parity.types import Column, KeySpec, LogicalType

SENTINEL = "concat(chr(92), 'N')"


class FakeRow:
    """Mimics `google.cloud.bigquery.Row`, which exposes `values()`."""

    def __init__(self, *values):
        """Hold one row's values."""
        self._values = values

    def values(self):
        """The row's values, in column order, as the real Row returns them."""
        return self._values


class FakeClient:
    """Records the SQL and the location, returns canned rows."""

    def __init__(self, rows=(), error=None):
        """Configure what every query returns, or raises."""
        self.rows, self.error, self.calls = list(rows), error, []

    def query_and_wait(self, sql, location=None):
        """Stand in for the jobs.query call the dialect makes."""
        self.calls.append((sql, location))
        if self.error is not None:
            raise self.error
        return [FakeRow(*r) for r in self.rows]


def _d(client=None, dataset="ds") -> BigQueryDialect:
    """A dialect wired to a fake client; nothing touches the network."""
    d = BigQueryDialect(side="A")
    d.project = "proj"
    d.default_schema = dataset
    d._location = "EU"
    d._client = client or FakeClient()
    return d


def _col(name: str, t: LogicalType, raw: str = "", tz=None) -> Column:
    """A column of the given logical type, for rendering."""
    return Column(name, t, raw, tz_aware=tz)


# ---------------------------------------------------------- connection string


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("bigquery://proj/ds", ("proj", "ds", None)),
        ("bigquery://proj", ("proj", "", None)),
        ("bigquery://proj/ds?location=EU", ("proj", "ds", "EU")),
        ("bigquery://proj/ds/", ("proj", "ds", None)),
        # Hand-parsed: urlparse().hostname would lower-case and split this.
        ("bigquery://example.com:Proj/ds", ("example.com:Proj", "ds", None)),
    ],
)
def test_connection_strings_parse(url, expected):
    """The grammar documented in the README, including its edge cases."""
    assert parse_bigquery_url(url) == expected


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("bigquery://", "needs a project"),
        ("bigquery:", "needs a project"),
        ("bigquery://p/d/extra", "3 path parts"),
        ("bigquery://p/d?locaton=EU", "unknown bigquery connection parameter"),
    ],
)
def test_bad_connection_strings_are_refused_not_guessed(url, message):
    """A typo in a parameter must fail loudly, not route jobs to the wrong
    region; and a malformed URL must say what the grammar is."""
    with pytest.raises(ValueError, match=message):
        parse_bigquery_url(url)


def test_get_dialect_routes_the_scheme_and_names_the_side():
    """`bigquery://` reaches this dialect, and a connect failure (here, a URL
    with no project) is reported once, naming the side."""
    with pytest.raises(ValueError) as exc:
        get_dialect("bigquery://", side="B")
    assert "side B" in str(exc.value)


# ------------------------------------------------------------------ rendering


def test_hash_expr_hex_casts_the_first_15_digits_of_the_md5():
    """TO_HEX because MD5 returns BYTES; 15 hex digits = 60 bits."""
    assert _d().hash_expr("'abc'") == (
        "cast(concat('0x', substr(to_hex(md5('abc')), 1, 15)) as int64)"
    )


def test_arithmetic_is_widened_to_bignumeric():
    """DIV truncates; BIGNUMERIC cannot overflow the key offset or the sum;
    an empty segment sums to 0, not NULL."""
    d = _d()
    assert d.int_div("x", "y") == "div(x, y)"
    assert d.wide_int("k") == "cast((k) as bignumeric)"
    assert d.sum_wide("h") == "coalesce(sum(cast((h) as bignumeric)), 0)"


def test_integer_literals_past_int64_become_bignumeric():
    """BigQuery cannot parse 2^64 as an integer literal; inside the range the
    SQL is plain digits, exactly as on every other engine."""
    d = _d()
    assert d.int_literal(2**63 - 1) == "9223372036854775807"
    assert d.int_literal(-(2**63)) == "-9223372036854775808"
    assert d.int_literal(2**64) == "bignumeric '18446744073709551616'"
    assert d.int_literal(-(2**63) - 1) == "bignumeric '-9223372036854775809'"


def test_the_segment_query_uses_the_bignumeric_span_for_a_full_range():
    """End to end through the base class's checksum SQL: the 2^64 span is a
    BIGNUMERIC literal, the bounds stay INT64, and the bound is inclusive."""
    d = _d()
    key = KeySpec((_col("id", LogicalType.INTEGER),), hashed=False)
    sql = d._segment_sql("t", key, [], -(2**63), 2**63, 4)
    assert "(bignumeric '18446744073709551616')" in sql
    assert "`id` >= -9223372036854775808 and `id` <= 9223372036854775807" in sql


def test_row_text_is_one_flat_array_join_however_wide():
    """No concat_ws in BigQuery, and no argument limit to nest around."""
    d = _d()
    cols = [_col(f"c{i}", LogicalType.INTEGER) for i in range(130)]
    text = d.row_text(cols)
    assert text.startswith("array_to_string([") and text.endswith("], chr(31))")
    assert text.count("array_to_string") == 1
    assert d.row_text([]) == "''"


def _bq_escaped(expr: str) -> str:
    """`expr` through BigQuery's text escape: collation stripped, backslash
    doubled, chr(31) as backslash-s."""
    return (
        f"replace(replace(collate({expr}, ''), chr(92), concat(chr(92), chr(92))), "
        "chr(31), concat(chr(92), 's'))"
    )


def test_normalize_renders_each_logical_type():
    """Every branch of the canonical-text encoding, null-safe."""
    d = _d()
    cases = [
        (_col("c", LogicalType.INTEGER), "cast(`c` as string)"),
        (_col("c", LogicalType.DECIMAL), "format('%.6f', round(`c`, 6))"),
        (
            _col("c", LogicalType.BOOLEAN),
            "case when `c` then 'true' when not `c` then 'false' end",
        ),
        (_col("c", LogicalType.DATE), "format_date('%E4Y-%m-%d', `c`)"),
        (
            _col("c", LogicalType.TIMESTAMP, "TIMESTAMP", tz=True),
            "format_timestamp('%E4Y-%m-%d %H:%M:%E6S', `c`, 'UTC')",
        ),
        (
            _col("c", LogicalType.TIMESTAMP, "DATETIME", tz=False),
            "format_datetime('%E4Y-%m-%d %H:%M:%E6S', `c`)",
        ),
        # Unknown awareness is treated as an instant and rendered in UTC.
        (
            _col("c", LogicalType.TIMESTAMP),
            "format_timestamp('%E4Y-%m-%d %H:%M:%E6S', `c`, 'UTC')",
        ),
        # Arbitrary text is escaped, collation stripped first.
        (_col("c", LogicalType.STRING), _bq_escaped("cast(`c` as string)")),
        (
            _col("c", LogicalType.UNKNOWN),
            _bq_escaped("if(`c` is null, null, format('%t', `c`))"),
        ),
    ]
    for column, inner in cases:
        assert d.normalize(column) == f"coalesce({inner}, {SENTINEL})", column


def test_float_branch_structure():
    """FLOAT is too long to pin verbatim (it builds the shortest repr), so pin
    its load-bearing parts: the tokens, the zero short-circuit, the 15/16/17
    significant-digit candidates with ties to even, the exact-integer split
    from 2^53, and the loud refusal past 2^116. The values themselves are
    checked live against the Python spec in test_float_canonical.py."""
    sql = _d().normalize(_col("c", LogicalType.FLOAT))
    # One WITH expression (RT-HB9): the column is read once, and the
    # BIGNUMERIC is bound only inside the band that needs it, because WITH
    # binds eagerly and the cast raises on Inf, NaN and huge magnitudes.
    assert sql.startswith(
        "coalesce(with(_fx as `c`, _fb as if(abs(_fx) >= 1e-7 and "
        "abs(_fx) < 9007199254740992, cast(_fx as bignumeric), null), "
    )
    assert sql.count("`c`") == 1
    assert (
        "case when is_inf(_fx) then (case when _fx > 0 then 'Infinity' "
        "else '-Infinity' end) when is_nan(_fx) then 'NaN' "
        "when abs(_fx) < 1e-7 then '0.000000' when abs(_fx) < 9007199254740992 "
    ) in sql
    for r, n in (("_f15", 14), ("_f16", 15), ("_f17", 16)):  # n - 1 significant digits
        assert f"{r} as round(_fb, {n} - _fe, 'ROUND_HALF_EVEN')" in sql
    assert sql.count("'ROUND_HALF_EVEN'") == 3  # each candidate written once
    assert "cast(cast(_fh as int64) as bignumeric)" in sql
    assert "_fh as trunc(_fx / 9007199254740992)" in sql
    assert "error(concat('parity: FLOAT value out of the comparable range" in sql
    assert len(sql) < 1300  # ~800 FLOAT64 columns inside the 1,024K limit


def test_float_scale_reaches_every_float_and_decimal_rounding():
    """A different --float-scale must move the zero threshold, the zero text,
    the final rounding and the format, or the scale would be applied twice."""
    d = BigQueryDialect(side="A", float_scale=2)
    sql = d.normalize(_col("c", LogicalType.FLOAT))
    assert "abs(_fx) < 1e-3 then '0.00'" in sql
    assert "abs(_fx) >= 1e-3" in sql
    assert "else _f17 end, 2))" in sql
    assert "'%.2f'" in sql and "'%.6f'" not in sql
    assert "round(`c`, 2)" in d.normalize(_col("c", LogicalType.DECIMAL))


def test_the_boolean_branch_never_uses_else():
    """`else 'false'` would turn a NULL boolean into 'false' - CLAUDE.md 4.3."""
    assert (
        " else "
        not in _d()
        .normalize(_col("c", LogicalType.BOOLEAN))
        .split("coalesce(")[1]
        .split(SENTINEL)[0]
    )


# -------------------------------------------------------- quoting and literals


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("plain", "`plain`"),
        ("a`b", "`a\\`b`"),
        ("c\\d", "`c\\\\d`"),
        ("x\\`; drop", "`x\\\\\\`; drop`"),
        ("ünï code", "`ünï code`"),
    ],
)
def test_quote_escapes_backslash_and_backtick(identifier, expected):
    """The injection boundary: a trailing backslash must not escape the
    closing backtick, and a backtick must not end the identifier."""
    assert _d().quote(identifier) == expected


def test_string_literals_escape_with_backslashes_not_doubling():
    """BigQuery does not accept '' as an escaped quote."""
    d = _d()
    assert d.string_literal("o'k") == "'o\\'k'"
    assert d.string_literal("a\\b") == "'a\\\\b'"


# ------------------------------------------------------------- name resolution


def test_table_names_resolve_to_three_parts():
    """table, dataset.table and project.dataset.table all qualify fully."""
    d = _d()
    assert d.qualify("t") == "`proj`.`ds`.`t`"
    assert d.qualify("other.t") == "`proj`.`other`.`t`"
    assert d.qualify("p2.other.t") == "`p2`.`other`.`t`"


def test_an_unqualified_table_with_no_default_dataset_is_refused():
    """Without a dataset there is nowhere to look; say so, naming the side."""
    with pytest.raises(ValueError, match=r"side A.*names no dataset"):
        _d(dataset="").qualify("t")


def test_a_four_part_name_is_refused():
    """Never guess at a name BigQuery itself would not accept."""
    with pytest.raises(ValueError, match="4 dot-separated parts"):
        _d().qualify("a.b.c.d")


def test_the_columns_catalog_is_the_datasets_own():
    """There is no global information_schema in BigQuery."""
    d = _d()
    assert d._columns_catalog("ds") == "`proj`.`ds`.INFORMATION_SCHEMA.COLUMNS"
    assert d._columns_catalog("p2.ds") == "`p2`.`ds`.INFORMATION_SCHEMA.COLUMNS"


# ---------------------------------------------------- metadata and the snapshot

TS = "2026-11-02 03:31:10.559880+00"


def test_query_passes_the_location_and_returns_tuples():
    """Every job runs in the data's location; rows come back as plain tuples."""
    client = FakeClient(rows=[(1, "a")])
    assert _d(client).query("select 1") == [(1, "a")]
    assert client.calls == [("select 1", "EU")]


def test_a_base_table_is_pinned_to_the_first_snapshot():
    """columns() fixes the snapshot and marks a base table; every later read of
    it goes through FOR SYSTEM_TIME AS OF that instant."""
    client = FakeClient(
        rows=[
            ("id", "INT64", "BASE TABLE", TS),
            ("amount", "NUMERIC(12, 2)", "BASE TABLE", TS),
            ("paid_at", "TIMESTAMP", "BASE TABLE", TS),
            ("made_at", "DATETIME", "BASE TABLE", TS),
        ]
    )
    d = _d(client)
    cols = d.columns("orders")
    assert [(c.name, c.logical_type, c.tz_aware) for c in cols] == [
        ("id", LogicalType.INTEGER, None),
        ("amount", LogicalType.DECIMAL, None),
        ("paid_at", LogicalType.TIMESTAMP, True),
        ("made_at", LogicalType.TIMESTAMP, False),
    ]
    sql = client.calls[0][0]
    assert "`proj`.`ds`.INFORMATION_SCHEMA.COLUMNS" in sql
    assert "INFORMATION_SCHEMA.TABLES" in sql and "current_timestamp()" in sql
    assert d.qualify("orders") == (
        f"`proj`.`ds`.`orders` for system_time as of timestamp '{TS}'"
    )
    # The snapshot is taken once: a later columns() call keeps the first one.
    client.rows = [("id", "INT64", "BASE TABLE", "2099-01-01 00:00:00.000000+00")]
    d.columns("ds.other")
    assert d.snapshot == TS


def test_a_view_is_never_pinned():
    """BigQuery silently ignores time travel on a view, so claiming a pin there
    would be claiming a consistency that is not real."""
    d = _d(FakeClient(rows=[("id", "INT64", "VIEW", TS)]))
    d.columns("v")
    assert d.snapshot == TS
    assert d.qualify("v") == "`proj`.`ds`.`v`"


def test_a_table_columns_never_saw_is_not_pinned():
    """Not proven to be a base table, so not pinned."""
    d = _d()
    d.snapshot = TS
    assert d.qualify("unseen") == "`proj`.`ds`.`unseen`"


def test_a_missing_table_names_the_side_and_hints_at_a_case_mismatch():
    """Table names are case-sensitive; the hint query looks in the same
    dataset, through the dataset's own INFORMATION_SCHEMA."""

    class Stubbed(BigQueryDialect):
        def query(self, sql):  # type: ignore[override]
            """Nothing for the exact name; a near miss for the hint."""
            self.seen.append(sql)
            if "lower(table_name)" in sql:
                return [("ds", "orders")]
            return []

    d = Stubbed(side="A")
    d.project, d.default_schema, d.seen = "proj", "ds", []
    with pytest.raises(ValueError) as exc:
        d.columns("Orders")
    message = str(exc.value)
    assert "side A: bigquery" in message and "table not found: Orders" in message
    assert "did you mean ds.orders" in message
    assert "`proj`.`ds`.INFORMATION_SCHEMA.COLUMNS" in d.seen[1]
    assert "lower('Orders')" in d.seen[1]


def test_an_unreadable_dataset_is_explained_not_passed_through():
    """BigQuery returns the same 403 for a missing dataset and a forbidden one."""
    d = _d(FakeClient(error=RuntimeError("403 Access Denied")))
    with pytest.raises(ValueError, match=r"side A.*cannot read dataset proj\.nope"):
        d.columns("nope.t")


def test_map_type_covers_what_information_schema_reports():
    """The spellings verified live, parameters stripped, ARRAY a LIST when its
    element has a rule, other composites unknown."""
    expected = {
        "INT64": LogicalType.INTEGER,
        "NUMERIC(12, 2)": LogicalType.DECIMAL,
        "NUMERIC": LogicalType.DECIMAL,
        "BIGNUMERIC(40, 10)": LogicalType.DECIMAL,
        "FLOAT64": LogicalType.FLOAT,
        "BOOL": LogicalType.BOOLEAN,
        "STRING(20)": LogicalType.STRING,
        "DATE": LogicalType.DATE,
        "DATETIME": LogicalType.TIMESTAMP,
        "TIMESTAMP": LogicalType.TIMESTAMP,
        "BYTES": LogicalType.BINARY,
        "TIME": LogicalType.TIME,
        "JSON": LogicalType.JSON,
        "ARRAY<INT64>": LogicalType.LIST,  # the list rule (RT-HB1)
        "ARRAY<STRUCT<x INT64>>": LogicalType.UNKNOWN,
        "STRUCT<x INT64, y STRING>": LogicalType.UNKNOWN,
    }
    for raw, logical in expected.items():
        assert map_type_bigquery(raw) is logical, raw


def test_tz_awareness_prefers_the_dialects_word_over_the_name():
    """BigQuery's instant type is plainly `TIMESTAMP`, which on PostgreSQL and
    DuckDB is the *naive* type; the explicit flag must win over the name, and
    the name-based inference must still work where no flag is set."""
    assert _is_tz_aware(Column("t", LogicalType.TIMESTAMP, "TIMESTAMP", tz_aware=True))
    assert not _is_tz_aware(
        Column("t", LogicalType.TIMESTAMP, "timestamp with time zone", tz_aware=False)
    )
    assert _is_tz_aware(Column("t", LogicalType.TIMESTAMP, "timestamp with time zone"))
    assert not _is_tz_aware(Column("t", LogicalType.TIMESTAMP, "timestamp"))
