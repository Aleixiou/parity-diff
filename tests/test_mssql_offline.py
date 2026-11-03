"""Offline unit tests for the SQL Server dialect and the hooks it added.

The live proof is `test_mssql.py`, which needs a server and so never runs in
CI. These pin the SQL the dialect generates, its connection-string parsing and
its connect/metadata logic through a stubbed driver, so every decision stays
covered on every push. Each rendering assertion records an expression the live
run verified against DuckDB; the reason is in the dialect's comments.

They also pin the other half of the bargain: the base-class hooks SQL Server
needed (`count`, `count_distinct_key`, `group_by`, `caveats`) leave every other
engine's SQL exactly as it was.
"""

from __future__ import annotations

import sys
import types

import pytest
from fakes import DictTable, FakeDialect

from parity.dialects.base import Dialect, get_dialect
from parity.dialects.bigquery_dialect import BigQueryDialect
from parity.dialects.duckdb_dialect import DuckDBDialect
from parity.dialects.mssql_dialect import (
    INVALID_UTF16_MARKER,
    MSSQL_FLOAT_MAX_SCALE,
    SQLServerDialect,
    _column,
    map_type_mssql,
    parse_mssql_url,
)
from parity.dialects.mysql_dialect import MySQLDialect
from parity.dialects.postgres_dialect import PostgresDialect
from parity.dialects.snowflake_dialect import SnowflakeDialect
from parity.engine import _is_tz_aware, diff
from parity.types import Column, KeySpec, LogicalType

SENTINEL = "N'\\N'"
INT_KEY = KeySpec((Column("id", LogicalType.INTEGER, "bigint"),), hashed=False)
TEXT_KEY = KeySpec((Column("code", LogicalType.STRING, "nvarchar"),), hashed=True)


class FakeCursor:
    """Records SQL; answers from the connection's canned rows, or raises."""

    def __init__(self, conn):
        """Attach to the fake connection."""
        self.conn = conn

    def execute(self, sql):
        """Record the statement and pick its canned answer."""
        self.conn.calls.append(sql)
        if self.conn.error is not None:
            raise self.conn.error
        self._rows = self.conn.answer(sql)

    def fetchall(self):
        """The canned rows for the last statement."""
        return self._rows

    def close(self):
        """Nothing to release."""


class FakeConn:
    """Stands in for an mssql-python connection."""

    def __init__(self, answer=lambda sql: [], error=None):
        """Configure how statements are answered."""
        self.answer, self.error = answer, error
        self.calls: list[str] = []
        self.autocommit = True
        self.events: list[str] = []

    def cursor(self):
        """A recording cursor."""
        return FakeCursor(self)

    def rollback(self):
        """Note the rollback; `close` must issue one before closing."""
        self.events.append("rollback")

    def close(self):
        """Note the close."""
        self.events.append("close")


def _d(answer=lambda sql: [], error=None, scale=6) -> SQLServerDialect:
    """A dialect wired to a fake connection; nothing touches the network."""
    d = SQLServerDialect(side="A", float_scale=scale)
    d._conn = FakeConn(answer, error)
    d.database = "warehouse"
    return d


def _col(name: str, raw: str, precision=None) -> Column:
    """A column as `columns()` would build it from INFORMATION_SCHEMA."""
    return _column(name, raw, precision)


def _utf16_safe(expr: str) -> str:
    """`expr` wrapped in the invalid-UTF-16 guard, whose own SQL is pinned by
    `test_n_types_render_invalid_utf16_as_a_marker_and_valid_text_unchanged`."""
    return _d()._utf16_safe(expr)


# ---------------------------------------------------------- connection string


@pytest.mark.parametrize(
    ("url", "odbc", "database"),
    [
        (
            "mssql://sa:pw@db.example:14330/wh",
            "Server={db.example,14330};Database={wh};UID={sa};PWD={pw}",
            "wh",
        ),
        (
            "sqlserver://u:p@host/wh",
            "Server={host,1433};Database={wh};UID={u};PWD={p}",
            "wh",
        ),
        # A `;` or `}` in a password cannot inject a keyword: values are braced.
        (
            "mssql://u:a%3BEncrypt%3Dno%7D@h/wh?trust_server_certificate=yes&encrypt=strict",
            (
                "Server={h,1433};Database={wh};UID={u};PWD={a;Encrypt=no}}};"
                "Encrypt={strict};TrustServerCertificate={yes}"
            ),
            "wh",
        ),
        (
            "mssql://u:p@h/wh?instance=SQLEXPRESS",
            "Server={h\\SQLEXPRESS};Database={wh};UID={u};PWD={p}",
            "wh",
        ),
        (
            "mssql://u:p@h:1500/wh?instance=I&application_intent=readonly&login_timeout=5",
            (
                "Server={h\\I,1500};Database={wh};UID={u};PWD={p};"
                "ApplicationIntent={readonly};LoginTimeout={5}"
            ),
            "wh",
        ),
        ("mssql://u@h/wh", "Server={h,1433};Database={wh};UID={u};PWD={}", "wh"),
    ],
)
def test_connection_strings_become_braced_odbc_strings(url, odbc, database):
    """The grammar documented in the README, including its edge cases."""
    assert parse_mssql_url(url) == (odbc, database)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("mssql:///wh", "needs a host"),
        ("mssql://u:p@h", "exactly one database"),
        ("mssql://u:p@h/a/b", "exactly one database"),
        ("mssql://h/wh", "needs a user"),
        ("mssql://u:p@h/wh?aplication_intent=readonly", "unknown mssql connection"),
    ],
)
def test_bad_connection_strings_are_refused_not_guessed(url, message):
    """A typo in a parameter must fail loudly rather than be dropped."""
    with pytest.raises(ValueError, match=message):
        parse_mssql_url(url)


def test_get_dialect_routes_both_schemes_and_names_the_side():
    """`mssql://` and `sqlserver://` reach this dialect; a parse failure is
    reported once, naming the side."""
    for scheme in ("mssql", "sqlserver"):
        with pytest.raises(ValueError) as exc:
            get_dialect(f"{scheme}://h/wh", side="B")
        assert str(exc.value).startswith("[side B: mssql] ")
        assert str(exc.value).count("side B") == 1


# -------------------------------------------------------------------- connect


def _probe(edition=3, schema="dbo", snapshot=1, utf8=1):
    """A canned answer for `_probe`'s one query."""
    return lambda sql: (
        [(edition, schema, snapshot, utf8)] if "serverproperty" in sql else []
    )


@pytest.fixture
def fake_driver(monkeypatch):
    """Install a fake `mssql_python` module whose `connect` returns `holder[0]`."""
    holder: list[FakeConn] = []
    module = types.ModuleType("mssql_python")

    def connect(odbc, autocommit):
        """Record how the driver was called."""
        holder[0].odbc, holder[0].opened_autocommit = odbc, autocommit
        return holder[0]

    module.connect = connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mssql_python", module)
    return holder


def test_connect_pins_snapshot_and_turns_autocommit_off(fake_driver):
    """With ALLOW_SNAPSHOT_ISOLATION on: SNAPSHOT is set while autocommit is
    still on, then autocommit goes off so the walk is one transaction."""
    fake_driver.append(FakeConn(_probe(schema="sales")))
    d = SQLServerDialect(side="A")
    d.connect("mssql://u:p@h/wh?trust_server_certificate=yes")
    conn = fake_driver[0]
    assert conn.opened_autocommit is True and conn.autocommit is False
    assert conn.calls[-1] == "set transaction isolation level snapshot"
    assert d.snapshot and d.default_schema == "sales" and d.database == "wh"
    assert d.caveats([]) == []
    d.close()
    assert conn.events == ["rollback", "close"]


def test_connect_without_snapshot_stays_read_committed_and_says_so(fake_driver):
    """Never REPEATABLE READ (held locks would block writers) and never NOLOCK."""
    fake_driver.append(FakeConn(_probe(snapshot=0, schema=None)))
    d = SQLServerDialect(side="B")
    d.connect("mssql://u:p@h/wh")
    assert fake_driver[0].calls[-1] == "set transaction isolation level read committed"
    assert d.default_schema == "dbo"
    (note,) = d.caveats([])
    assert "ALLOW_SNAPSHOT_ISOLATION OFF" in note and "'wh'" in note


@pytest.mark.parametrize(
    ("probe", "message"),
    [
        (_probe(edition=6), "engine edition 6"),
        (_probe(edition=11), "Fabric"),
        (_probe(utf8=0), "no UTF-8 collations"),
    ],
)
def test_connect_refuses_what_it_cannot_compare_exactly(fake_driver, probe, message):
    """Synapse and Fabric isolate differently; a server without UTF-8
    collations cannot hash the bytes every other engine hashes."""
    fake_driver.append(FakeConn(probe))
    with pytest.raises(ValueError, match=message) as exc:
        SQLServerDialect(side="A").connect("mssql://u:p@h/wh")
    assert "side A: mssql" in str(exc.value)


# ----------------------------------------------------------- query and close


def test_query_refuses_anything_but_select():
    """Read-only by construction: the last line behind a read-only login."""
    d = _d()
    for sql in (
        "delete from t",
        "  insert into t values (1)",
        "exec sp_who",
        "with x as (select 1) delete from t",
    ):
        with pytest.raises(ValueError, match="non-SELECT"):
            d.query(sql)
    assert d._conn.calls == []
    assert d.query("  SELECT 1") == []


def test_query_turns_the_float_refusal_into_an_error_naming_the_side():
    """The FLOAT guard raises through a failed int conversion; the user sees
    the guard's own words and the side, not a conversion error."""
    error = RuntimeError(
        "Conversion failed when converting the nvarchar value 'parity: FLOAT value "
        "out of the comparable range (|x| >= 2^116, about 8.3e34) - refusing to round "
        "it: 1.0000000000000000e+035' to data type int."
    )
    with pytest.raises(ValueError, match=r"^\[side A: mssql\] parity: FLOAT.*e\+035$"):
        _d(error=error).query("select 1")
    with pytest.raises(RuntimeError, match="deadlock"):
        _d(error=RuntimeError("deadlock")).query("select 1")


def test_close_rolls_back_even_if_rollback_fails():
    """Nothing is ever committed, and the connection still closes."""
    d = _d()

    def boom():
        """A rollback on a dead connection."""
        raise RuntimeError("gone")

    d._conn.rollback = boom
    with pytest.raises(RuntimeError):
        d.close()
    assert d._conn.events == ["close"]


# ------------------------------------------------------------------- metadata


def test_columns_reads_upper_case_catalog_names_and_precision():
    """Upper case or a case-sensitive database rejects the catalog; datetime2
    precision folds into raw_type; datetimeoffset is marked an instant."""
    rows = [("id", "bigint", None), ("t", "datetime2", 7), ("o", "datetimeoffset", 3)]
    d = _d(lambda sql: rows)
    cols = d.columns("sales.orders")
    sql = d._conn.calls[0]
    assert "from INFORMATION_SCHEMA.COLUMNS where TABLE_SCHEMA = N'sales'" in sql
    assert "TABLE_NAME = N'orders' order by ORDINAL_POSITION" in sql
    assert [(c.name, c.raw_type, c.tz_aware) for c in cols] == [
        ("id", "bigint", None),
        ("t", "datetime2(7)", False),
        ("o", "datetimeoffset(3)", True),
    ]
    assert _is_tz_aware(cols[2]) and not _is_tz_aware(cols[1])


def test_a_missing_table_names_near_misses_and_the_grant_hint():
    """Unqualified names use the login's default schema; the near-miss query
    is upper case too; the message mentions a missing GRANT."""
    d = _d(lambda sql: [("dbo", "Orders")] if "lower(" in sql else [])
    with pytest.raises(ValueError) as exc:
        d.columns("orders")
    message = str(exc.value)
    assert "side A: mssql" in message and "dbo.Orders" in message
    assert "GRANT SELECT" in message
    assert "select distinct TABLE_SCHEMA, TABLE_NAME" in d._conn.calls[1]

    def failing(sql):
        """The near-miss query fails; the lookup itself finds nothing."""
        if "lower(" in sql:
            raise RuntimeError("no catalog")
        return []

    with pytest.raises(ValueError, match=r"table not found.*GRANT SELECT"):
        _d(failing).columns("x")


def test_quoting_is_the_injection_boundary():
    """Brackets with `]` doubled; literals are N'' with `'` doubled."""
    d = _d()
    assert d.quote("a]b") == "[a]]b]"
    assert d.qualify("s.t") == "[s].[t]"
    assert d.string_literal("it's") == "N'it''s'"


@pytest.mark.parametrize(
    ("raw", "logical"),
    [
        ("bigint", LogicalType.INTEGER),
        ("TINYINT", LogicalType.INTEGER),
        ("money", LogicalType.DECIMAL),
        ("numeric", LogicalType.DECIMAL),
        ("real", LogicalType.FLOAT),
        ("bit", LogicalType.BOOLEAN),
        ("date", LogicalType.DATE),
        ("datetime2", LogicalType.TIMESTAMP),
        ("smalldatetime", LogicalType.TIMESTAMP),
        ("datetimeoffset", LogicalType.TIMESTAMP),
        ("nchar", LogicalType.STRING),
        ("sysname", LogicalType.STRING),
        ("uniqueidentifier", LogicalType.STRING),
        # rowversion, not a point in time - the shared map would say TIMESTAMP.
        ("timestamp", LogicalType.UNKNOWN),
        ("time", LogicalType.UNKNOWN),
        ("varbinary", LogicalType.UNKNOWN),
        ("xml", LogicalType.UNKNOWN),
    ],
)
def test_type_names_map_with_the_sql_server_traps(raw, logical):
    """`bit` is boolean here (a bit string on PostgreSQL); `timestamp` is not."""
    assert map_type_mssql(raw) is logical


# ---------------------------------------------------------------- rendering


def test_hash_is_md5_of_utf8_bytes_via_hex():
    """UTF-8 through a _UTF8 collation; 15 hex digits behind a 0."""
    assert _d().hash_expr("'abc'") == (
        "convert(bigint, convert(varbinary(8), '0' + left(convert(varchar(32), "
        "hashbytes('MD5', cast(cast(('abc') as nvarchar(max)) collate "
        "Latin1_General_100_BIN2_UTF8 as varchar(max))), 2), 15), 2))"
    )


def test_arithmetic_never_uses_a_rounding_division():
    """int_div divides an exact multiple; decimal(38,0) everywhere else."""
    d = _d()
    assert d.int_div("x", "y") == "cast(((x) - (x) % (y)) / (y) as bigint)"
    assert d.wide_int("k") == "cast((k) as decimal(38,0))"
    assert d.sum_wide("h") == "coalesce(sum(cast((h) as decimal(38,0))), 0)"


@pytest.mark.parametrize(
    ("column", "expr"),
    [
        (_col("c", "int"), "convert(nvarchar(max), [c])"),
        (_col("c", "decimal"), "convert(nvarchar(max), cast([c] as decimal(38,6)))"),
        (_col("c", "money"), "convert(nvarchar(max), cast([c] as decimal(38,6)))"),
        (
            _col("c", "bit"),
            "case when [c] = 1 then N'true' when [c] = 0 then N'false' end",
        ),
        (_col("c", "date"), "convert(nvarchar(max), [c], 23)"),
        (_col("c", "datetime"), "convert(nvarchar(max), [c], 121) + N'000'"),
        (
            _col("c", "datetime2", 7),
            "left(convert(nvarchar(max), cast([c] as datetime2(7)), 121), 26)",
        ),
        (
            _col("c", "smalldatetime"),
            "left(convert(nvarchar(max), cast([c] as datetime2(7)), 121), 26)",
        ),
        (
            _col("c", "datetimeoffset", 7),
            (
                "left(convert(nvarchar(max), cast(switchoffset([c], '+00:00') "
                "as datetime2(7)), 121), 26)"
            ),
        ),
        (_col("c", "char"), "rtrim(cast([c] as nvarchar(max)))"),
        # N-types guard against unpaired surrogates (RT-E9); see the test below.
        (_col("c", "nchar"), _utf16_safe("rtrim(cast([c] as nvarchar(max)))")),
        (_col("c", "uniqueidentifier"), "lower(convert(nvarchar(max), [c]))"),
        (_col("c", "nvarchar"), _utf16_safe("cast([c] as nvarchar(max))")),
        (_col("c", "varchar"), "cast([c] as nvarchar(max))"),
        (
            _col("c", "timestamp"),
            "convert(nvarchar(max), convert(varbinary(max), [c]), 1)",
        ),
        (
            _col("c", "varbinary"),
            "convert(nvarchar(max), convert(varbinary(max), [c]), 1)",
        ),
        (_col("c", "geography"), "[c].ToString()"),
        (_col("c", "time", 7), "cast([c] as nvarchar(max))"),
    ],
)
def test_every_type_renders_as_nvarchar_max_inside_the_coalesce(column, expr):
    """The exact SQL verified live, per type. nvarchar(max) everywhere is what
    keeps CONCAT_WS from truncating the row text at 4,000 characters."""
    assert _d().normalize(column) == f"coalesce({expr}, {SENTINEL})"


def test_n_types_render_invalid_utf16_as_a_marker_and_valid_text_unchanged():
    """RT-E9: an unpaired surrogate became U+FFFD on the way to UTF-8, so two
    different values hashed alike. The guard returns valid text untouched (no
    checksum moves) and anything else as the marker plus its own bytes;
    `varchar`, which cannot hold a surrogate, is not guarded at all."""
    sql = _d().normalize(_col("c", "nvarchar"))
    v = "cast([c] as nvarchar(max))"
    assert f"case when {v} collate Latin1_General_BIN2 not like " in sql
    assert "nchar(55296)" in sql and "nchar(57343)" in sql
    assert f"datalength(convert(varbinary(max), {v})) then {v} " in sql
    assert f"N'{INVALID_UTF16_MARKER}' + convert(nvarchar(max), " in sql
    assert "varbinary" not in _d().normalize(_col("c", "varchar"))


def test_float_is_the_two_band_shortest_repr_construction():
    """The pieces the live sample run depends on: the short-circuit to zero,
    the exact-decimal bands, the round-trip test, ties to even, the exact
    integer branch and the refusal."""
    sql = _d().normalize(_col("x", "float"))
    x = "cast([x] as float)"
    assert sql.startswith(f"coalesce(case when abs({x}) < 1e-7 then N'0.000000' ")
    assert f"cast({x} as decimal(38,37))" in sql and f"cast({x} as decimal(38,21))" in sql
    assert f"when abs({x}) < power(cast(2 as float), 53)" in sql
    assert f"then convert(nvarchar(max), cast({x} as decimal(38,0))) + N'.000000'" in sql
    assert (
        "refusing to round it: ' + convert(nvarchar(30), cast([x] as float), 3) "
        "+ N' in column ''x''' as int)" in sql
    )
    # Ties to even at 16 and 17 digits, in both bands; the 16-digit candidate
    # is written twice (tested, then returned), the 17-digit one once.
    assert sql.count("% 2 = 0") == 6
    assert _d(scale=0).normalize(_col("x", "real")).count("N'.") == 0


def test_float_and_decimal_scales_past_what_sql_server_holds_are_refused():
    """Past 18 places the FLOAT tie test would lose a digit; past 38 no
    decimal exists. Both refuse, naming the column and side."""
    with pytest.raises(
        ValueError, match=rf"side A: mssql.*'x'.*maximum is {MSSQL_FLOAT_MAX_SCALE}"
    ):
        _d(scale=MSSQL_FLOAT_MAX_SCALE + 1).normalize(_col("x", "float"))
    assert "decimal(38,18)" in _d(scale=18).normalize(_col("x", "float"))
    with pytest.raises(ValueError, match="maximum decimal scale is 38"):
        _d(scale=39).normalize(_col("d", "decimal"))


def test_concat_ws_never_gets_a_single_value():
    """T-SQL refuses CONCAT_WS with one value; one part is returned as itself,
    including the remainder group of a 65-part row."""
    d = _d()
    one = [Column("a", LogicalType.INTEGER, "int")]
    assert d.row_text(one) == f"coalesce(convert(nvarchar(max), [a]), {SENTINEL})"
    cols = [Column(f"c{i}", LogicalType.INTEGER, "int") for i in range(65)]
    text = d.row_text(cols)
    assert text.startswith("concat_ws(nchar(31), concat_ws(nchar(31), ")
    assert text.endswith(f"), coalesce(convert(nvarchar(max), [c64]), {SENTINEL}))")
    assert d.row_text([]) == "''"


def test_checksum_and_stats_queries_use_t_sql_spellings():
    """group by the expression, count_big, and uniqueness by SHA-256 for a
    hashed key (text equality follows the collation)."""
    d = _d(lambda sql: [(1, 9, 9, 9, 9)])
    sql = d._segment_sql("s.t", INT_KEY, [], 1, 100, 4)
    bucket = d.int_div(f"({d.wide_int('[id]')} - (1)) * 4", "(99)")
    assert bucket.startswith("cast((((cast(([id]) as decimal(38,0)) - (1)) * 4) - ")
    assert sql.startswith(f"select {bucket} as seg, count_big(*), ")
    assert sql.endswith(f"group by {bucket}")
    assert "group by 1" not in sql
    d.key_stats("s.t", INT_KEY)
    assert "count_big(*), count_big([id]), count_big(distinct [id])" in d._conn.calls[-1]
    d.key_stats("s.t", TEXT_KEY)
    assert "count_big(distinct hashbytes('SHA2_256', cast(cast((" in d._conn.calls[-1]


def test_caveats_report_sub_microsecond_columns():
    """datetime2(7)/datetimeoffset(7) lose their seventh digit, and say so;
    lower precisions are exact and stay quiet."""
    d = _d()
    # time(7) is compared as raw text, all seven digits, so it is not listed.
    cols = [
        _col("a", "datetime2", 7),
        _col("b", "datetime2", 6),
        _col("c", "datetimeoffset", 7),
        _col("d", "time", 7),
    ]
    (note,) = d.caveats(cols)
    assert "['a', 'c']" in note and "microseconds" in note


# --------------------------------------------- hooks: other engines unchanged


@pytest.mark.parametrize(
    "dialect",
    [PostgresDialect, DuckDBDialect, MySQLDialect, SnowflakeDialect, BigQueryDialect],
)
def test_the_new_hooks_leave_every_other_engine_sql_as_it_was(dialect):
    """`group by 1`, `count(*)`, `count(distinct identity)` and no caveats -
    exactly the text the shared queries always had, so no checksum moves."""
    d: Dialect = dialect(side="A")
    if isinstance(d, MySQLDialect):
        # MySQL counts hashed keys by their bytes since RT-E5 / RT-E8: its
        # default collation merged 'a'/'A' and ignored the chr(31) separator.
        # Only the uniqueness count changed - no checksum or row text did.
        assert d.count_distinct_key(TEXT_KEY, "x") == "count(distinct cast(x as binary))"
        assert d.count_distinct_key(INT_KEY, "x") == "count(distinct x)"
        return
    if isinstance(d, BigQueryDialect):
        d.project, d.default_schema = "p", "d"
    seen: list[str] = []
    d.query = lambda sql: seen.append(sql) or [(1, 9, 9, 9, 9)]  # type: ignore[method-assign]
    sql = d._segment_sql("s.t", INT_KEY, [], 1, 100, 4)
    assert sql.endswith("group by 1") and " as seg, count(*), " in sql
    d.key_stats("s.t", TEXT_KEY)
    identity = d.key_identity(TEXT_KEY)
    assert f"count(*), count({identity}), count(distinct {identity}) from" in seen[-1]
    assert d.caveats([Column("t", LogicalType.TIMESTAMP, "datetime2(7)")]) == []


def test_the_engine_prints_a_dialect_caveat_naming_the_side():
    """`caveats` reaches the output as a warning (CLAUDE.md section 8), once
    per side, with the key and compared columns passed in."""
    cols = [Column("v", LogicalType.STRING, "text")]
    table = DictTable(cols, {1: ("a",), 2: ("b",)})

    class Noted(FakeDialect):
        def caveats(self, columns):
            """Report what was passed, so the test can see it."""
            return [f"saw {[c.name for c in columns]}"]

    a, b = Noted(table, side="A"), FakeDialect(table, side="B")
    result = diff(a, b, "t", "t", key="id")
    assert result.identical
    assert "side A (fake): saw ['id', 'v']" in result.warnings
    assert not [w for w in result.warnings if w.startswith("side B")]
