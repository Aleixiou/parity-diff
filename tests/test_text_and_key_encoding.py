r"""Escaped text and exact key text, agreed across every reachable engine.

Two encoding rules, each closing a false match the red team found:

1. **Text is escaped.** STRING values and the text of unmapped types double
   every backslash, then turn chr(31) into `\s`. Without it the string `\N`
   was NULL and a chr(31) inside a value moved a field boundary (RT-A1..A4,
   B1, B3, E1, E2, F9). `escape_canonical_text` is the spec; every engine must
   equal it byte for byte, and text holding neither character must render
   exactly as it always did, so ordinary checksums do not move.
2. **Keys are exact.** A key column's text is its identity, so it is never
   rounded: DECIMAL keys render their exact value without trailing zeros
   (`canonical_decimal_key_text`), sub-microsecond timestamp keys keep their
   digits, FLOAT keys are refused (RT-A18, B5, B6).

Values are built on the server from code points, not bound as parameters or
spelled as literals, so no driver or literal-escaping setting can touch the
two characters under test. Nothing here creates a table: each probe selects
from a one-row derived table, so it shares no objects with any other run.
Unreachable engines skip, never fail. Every agreement is paired with a
planted difference that must be seen (CLAUDE.md section 8).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from decimal import Decimal

import pytest
from conftest import (
    BIGQUERY_URL,
    MSSQL_URL,
    MYSQL_URL,
    PG_URL,
    duckdb_write,
    open_duckdb,
)

from parity.dialects.base import (
    NULL_SENTINEL,
    Dialect,
    canonical_decimal_key_text,
    decode_canonical_field,
    escape_canonical_text,
    get_dialect,
)
from parity.dialects.bigquery_dialect import BigQueryDialect
from parity.dialects.duckdb_dialect import DuckDBDialect
from parity.dialects.mssql_dialect import SQLServerDialect
from parity.dialects.mysql_dialect import MySQLDialect
from parity.dialects.postgres_dialect import PostgresDialect
from parity.dialects.snowflake_dialect import SnowflakeDialect
from parity.engine import diff
from parity.types import Column, KeySpec, LogicalType

SEP = "\x1f"
BS = "\\"

#: Adversarial text: the sentinel, near-sentinels, the separator, the escape
#: sequences themselves, and ordinary text that must not change.
STRINGS = [
    "",
    "plain",
    BS,
    NULL_SENTINEL,
    BS + BS + "N",
    "N",
    f"a{SEP}b",
    SEP,
    SEP + SEP,
    BS + "s",
    BS + SEP,
    SEP + BS,
    "C:" + BS + "path" + BS + "to",
    "tab\tnew\nline",
    "é€😀",
]


def _row_hash(text: str) -> int:
    """The 60-bit row hash every engine computes, in Python."""
    return int(hashlib.md5(text.encode()).hexdigest()[:15], 16)


# ------------------------------------------------------------------ spec


def test_escape_is_identity_on_ordinary_text():
    """Text without a backslash or chr(31) is untouched, so its checksum does
    not move - and the planted control: text with one does change."""
    for s in ("", "plain", "tab\tnew\nline", "é€😀", "N", "100%"):
        assert escape_canonical_text(s) == s
    assert escape_canonical_text(BS) != BS
    assert escape_canonical_text(SEP) != SEP


def test_escape_never_yields_the_sentinel_or_the_separator():
    """The two properties that make row text unambiguous."""
    for s in STRINGS:
        e = escape_canonical_text(s)
        assert e != NULL_SENTINEL
        assert SEP not in e


def test_escape_is_injective_and_decodes_back():
    """Distinct values stay distinct, and display decoding recovers them."""
    escaped = [escape_canonical_text(s) for s in STRINGS]
    assert len(set(escaped)) == len(STRINGS)
    for s, e in zip(STRINGS, escaped, strict=True):
        assert decode_canonical_field(e) == s
    assert decode_canonical_field(NULL_SENTINEL) is None


def test_separator_inside_values_no_longer_smears_rows():
    """('a\\x1fb', 'c') and ('a', 'b\\x1fc') joined to one text before."""
    a = SEP.join(escape_canonical_text(v) for v in (f"a{SEP}b", "c"))
    b = SEP.join(escape_canonical_text(v) for v in ("a", f"b{SEP}c"))
    assert a != b
    assert _row_hash(a) != _row_hash(b)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("1.50000000", "1.5"),
        ("-0.50", "-0.5"),
        ("100", "100"),
        ("100.000", "100"),
        ("0.000", "0"),
        ("-0.000", "0"),
        ("0.00000001", "0.00000001"),
        ("1E+3", "1000"),
        ("123456789012345678901234567890.123", "123456789012345678901234567890.123"),
    ],
)
def test_decimal_key_spec(value, text):
    """The Python spec: exact, plain, no trailing zeros, never `-0`."""
    assert canonical_decimal_key_text(Decimal(value)) == text


# ---------------------------------------------------- SQL text, offline

ALL_DIALECTS: list[type[Dialect]] = [
    PostgresDialect,
    DuckDBDialect,
    MySQLDialect,
    SQLServerDialect,
    BigQueryDialect,
    SnowflakeDialect,
]


@pytest.mark.parametrize("cls", ALL_DIALECTS, ids=lambda c: c.name)
def test_no_backslash_literal_reaches_the_sql(cls):
    """RT-D1: a backslash inside a string literal is read differently under
    PostgreSQL's standard_conforming_strings=off and MySQL's sql_mode, so
    none may appear in rendering SQL. SQL Server keeps `N'\\N'`: T-SQL has
    no backslash escapes and no setting that adds them."""
    d = cls()
    cols = [Column("c", t, "") for t in LogicalType if t is not LogicalType.FLOAT]
    sql = d.row_text(cols) + d.null_sentinel_sql()
    if cls is SQLServerDialect:
        sql = sql.replace(f"N'{NULL_SENTINEL}'", "")
    assert BS not in sql


@pytest.mark.parametrize("cls", ALL_DIALECTS, ids=lambda c: c.name)
def test_text_branches_are_escaped(cls):
    """STRING and the unmapped-type fallback go through `escape_text`."""
    d = cls()
    for t in (LogicalType.STRING, LogicalType.UNKNOWN):
        assert "replace(replace(" in d.normalize(Column("c", t, ""))
    for t in (LogicalType.INTEGER, LogicalType.DATE, LogicalType.BOOLEAN):
        assert "replace(" not in d.normalize(Column("c", t, ""))


@pytest.mark.parametrize("cls", ALL_DIALECTS, ids=lambda c: c.name)
def test_float_key_is_refused_naming_side_table_and_column(cls):
    """RT-B5: a FLOAT key would be matched by rounded text; it is refused."""
    d = cls(side="B")
    key = KeySpec((Column("price", LogicalType.FLOAT, "double"),), hashed=True)
    with pytest.raises(
        ValueError, match=r"side B.*orders.*'price'.*cannot be used as a key"
    ):
        d.check_key("orders", key)
    with pytest.raises(ValueError, match="cannot be used as a key"):
        d.key_identity(key)


@pytest.mark.parametrize("cls", ALL_DIALECTS, ids=lambda c: c.name)
def test_hashed_key_counts_null_components_as_missing(cls):
    """RT-B2: the key_stats query counts a row as keyed only when every key
    column is set, so a NULL part is refused rather than becoming `\\N`."""
    d = cls()
    key = KeySpec(
        (Column("a", LogicalType.STRING), Column("b", LogicalType.INTEGER)), hashed=True
    )
    sql: list[str] = []

    def capture(q: str) -> list[tuple]:
        """Record the SQL and answer: 3 rows, 2 of them fully keyed."""
        sql.append(q)
        return [(1, 2, 3, 2, 2)]

    d.query = capture  # type: ignore[method-assign]
    d.qualify = lambda t: t  # type: ignore[method-assign]
    stats = d.key_stats("t", key)
    assert stats.null_keys == 1
    a, b = d.quote("a"), d.quote("b")
    assert f"case when {a} is not null and {b} is not null then 1 end" in sql[0]


# ------------------------------------------------------------- engines


def _lit(d: Dialect, s: str) -> str:
    """`s` as SQL built from code points for the two special characters."""
    if s == "":
        return "N''" if isinstance(d, SQLServerDialect) else "''"
    parts: list[str] = []
    run = ""
    for ch in s:
        # Control characters too: a BigQuery '...' literal cannot hold a raw
        # newline.
        if ch in (BS, "'") or ord(ch) < 32 or ord(ch) > 127:
            if run:
                parts.append(
                    ("N'" if isinstance(d, SQLServerDialect) else "'") + run + "'"
                )
                run = ""
            parts.append(_chr(d, ord(ch)))
        else:
            run += ch
    if run:
        parts.append(("N'" if isinstance(d, SQLServerDialect) else "'") + run + "'")
    out = parts[0]
    for p in parts[1:]:
        out = d.concat_sql(out, p)
    return out


def _chr(d: Dialect, n: int) -> str:
    """One character from its code point, in this engine's spelling."""
    if isinstance(d, MySQLDialect):
        # char() takes bytes, not code points; as UTF-32 one int is one
        # code point, then converted to the connection's utf8mb4.
        return f"convert(char({n} using utf32) using utf8mb4)"
    if isinstance(d, SQLServerDialect):
        if n > 0xFFFF:  # a surrogate pair: nchar() takes one UTF-16 unit
            n -= 0x10000
            return f"(nchar({0xD800 + (n >> 10)}) + nchar({0xDC00 + (n & 0x3FF)}))"
        return f"nchar({n})"
    if isinstance(d, BigQueryDialect):
        return f"code_points_to_string([{n}])"
    return f"chr({n})"


def _text_type(d: Dialect) -> str:
    """The text type a probe value is cast to on this engine."""
    return {
        "postgres": "text",
        "duckdb": "varchar",
        "mysql": "char",
        "mssql": "nvarchar(100)",
        "bigquery": "string",
    }[d.name]


def _select(d: Dialect, values: dict[str, str], exprs: list[str]) -> tuple:
    """Evaluate `exprs` over one derived row whose columns hold `values`."""
    inner = ", ".join(f"{v} as {d.quote(k)}" for k, v in values.items())
    return d.query(f"select {', '.join(exprs)} from (select {inner}) as s")[0]


def _open_each() -> Iterator[tuple[str, Callable[[], Dialect]]]:
    """Each engine this run could reach, with how to open it."""
    yield "duckdb", lambda: _duck_memory()
    yield "postgres", lambda: get_dialect(PG_URL, side="A")
    yield "mysql", lambda: get_dialect(MYSQL_URL, side="A")
    if MSSQL_URL:
        yield "mssql", lambda: get_dialect(MSSQL_URL, side="A")
    if BIGQUERY_URL:
        yield "bigquery", lambda: get_dialect(BIGQUERY_URL, side="A")


def _duck_memory() -> Dialect:
    """An in-memory DuckDB dialect; probes need no file."""
    pytest.importorskip("duckdb")
    return get_dialect("duckdb:///:memory:", side="A")


@pytest.fixture(scope="module", params=[name for name, _ in _open_each()])
def engine(request) -> Iterator[Dialect]:
    """Each reachable engine in turn; skipped when it is not."""
    opener = dict(_open_each())[request.param]
    try:
        d = opener()
    except Exception as exc:  # unreachable engines skip, never fail
        pytest.skip(f"{request.param} not reachable: {type(exc).__name__}")
    yield d
    d.close()


def _cast_text(d: Dialect, lit: str) -> str:
    """`lit` cast to this engine's text type, so the column reads as text."""
    return f"cast({lit} as {_text_type(d)})"


def test_engine_escapes_text_exactly_like_the_spec(engine):
    """Every adversarial string renders as `escape_canonical_text` says, on
    every engine - and the planted controls: no rendering equals the NULL
    sentinel, all are distinct, and a real NULL is the sentinel."""
    d = engine
    col = Column("c", LogicalType.STRING, _text_type(d))
    got = []
    for s in STRINGS:
        (text,) = _select(d, {"c": _cast_text(d, _lit(d, s))}, [d.normalize(col)])
        assert text == escape_canonical_text(s), (d.name, s)
        got.append(text)
    assert NULL_SENTINEL not in got
    assert len(set(got)) == len(got)
    (null_text,) = _select(d, {"c": _cast_text(d, "null")}, [d.normalize(col)])
    assert null_text == NULL_SENTINEL


def test_engine_row_hash_separates_shifted_separators(engine):
    """('a\\x1fb', 'c') and ('a', 'b\\x1fc'): one row before, two now. And the
    hash of ordinary text equals the unescaped Python hash, so it did not
    move."""
    d = engine
    cols = [Column(n, LogicalType.STRING, _text_type(d)) for n in ("x", "y")]

    def row(x: str, y: str) -> tuple:
        """Row text and row hash of (x, y) on this engine."""
        values = {"x": _cast_text(d, _lit(d, x)), "y": _cast_text(d, _lit(d, y))}
        return _select(d, values, [d.row_text(cols), d.row_hash(cols)])

    t1, h1 = row(f"a{SEP}b", "c")
    t2, h2 = row("a", f"b{SEP}c")
    assert t1 != t2
    assert int(h1) != int(h2)
    assert int(h1) == _row_hash(t1)
    t3, h3 = row("plain", "é€")
    assert t3 == f"plain{SEP}é€"
    assert int(h3) == _row_hash(f"plain{SEP}é€")


#: (literal, a declared type wide enough for it, as (precision, scale)).
DECIMAL_KEYS = [
    ("1.5", (18, 8)),
    ("1.50", (10, 2)),
    ("1.00000001", (18, 8)),
    ("1.00000002", (18, 8)),
    ("-0.5", (10, 4)),
    ("0", (10, 4)),
    ("42", (10, 0)),
    ("42", (12, 3)),
    ("100", (10, 0)),
    ("100.0010", (12, 4)),
    ("-12345678901234567890.123456789", (38, 9)),
    ("0.000000001", (20, 9)),
]


def _decimal_type(d: Dialect, p: int, s: int) -> str:
    """A DECIMAL type of this precision and scale, or BigQuery's nearest."""
    if d.name == "bigquery":
        return "bignumeric" if s > 9 or p - s > 29 else "numeric"
    return f"decimal({p},{s})"


def test_engine_renders_decimal_keys_exactly(engine):
    """A DECIMAL key is its exact value, whatever the declared scale: the
    same text on every engine, `1.5` for both numeric(10,2) and
    decimal(18,8). The planted control: 1.00000001 and 1.00000002, which
    the float scale merged as key text (RT-A18), stay distinct."""
    d = engine
    got = {}
    for lit, (p, s) in DECIMAL_KEYS:
        col = Column("k", LogicalType.DECIMAL, _decimal_type(d, p, s))
        value = f"cast('{lit}' as {_decimal_type(d, p, s)})"
        (text,) = _select(d, {"k": value}, [d.normalize_key(col)])
        assert text == canonical_decimal_key_text(Decimal(lit)), (d.name, lit, p, s)
        got[(lit, p, s)] = text
    assert got[("1.00000001", 18, 8)] != got[("1.00000002", 18, 8)]
    assert got[("1.5", 18, 8)] == got[("1.50", 10, 2)]
    col = Column("k", LogicalType.DECIMAL, _decimal_type(d, 10, 2))
    (null_text,) = _select(
        d, {"k": f"cast(null as {_decimal_type(d, 10, 2)})"}, [d.normalize_key(col)]
    )
    assert null_text == NULL_SENTINEL


@pytest.mark.mssql
def test_mssql_money_keys_render_all_four_places():
    """money converts to text at two places by default; a key needs all four."""
    if not MSSQL_URL:
        pytest.skip("PARITY_TEST_MSSQL is not set")
    d = get_dialect(MSSQL_URL, side="A")
    try:
        for raw in ("money", "smallmoney"):
            col = Column("k", LogicalType.DECIMAL, raw)
            a, b = (
                _select(d, {"k": f"cast('{v}' as {raw})"}, [d.normalize_key(col)])[0]
                for v in ("1.2345", "1.2346")
            )
            assert (a, b) == ("1.2345", "1.2346")
    finally:
        d.close()


#: Sub-microsecond instants, as 100 ns ticks so SQL Server can hold them too.
NS_KEYS = [
    ("2024-01-01 00:00:00.0000001", "2024-01-01 00:00:00.000000100"),
    ("2024-01-01 00:00:00.0000002", "2024-01-01 00:00:00.000000200"),
    ("2024-01-01 00:00:00.1234567", "2024-01-01 00:00:00.123456700"),
    ("2024-01-01 00:00:00.1234560", "2024-01-01 00:00:00.123456"),
    ("1969-12-31 23:59:59.9999999", "1969-12-31 23:59:59.999999900"),
]


def test_duckdb_and_mssql_agree_on_nanosecond_keys(tmp_path):
    """TIMESTAMP_NS on DuckDB and datetime2(7) on SQL Server render the same
    instant as the same key, at nanoseconds only when there are any - so a
    microsecond-aligned key still matches a microsecond engine's key. The
    planted control: two keys 100 ns apart, which were one key (RT-B6)."""
    pytest.importorskip("duckdb")
    duck = _duck_memory()
    renderings = {}
    try:
        col = Column("k", LogicalType.TIMESTAMP, "TIMESTAMP_NS")
        for value, text in NS_KEYS:
            (got,) = _select(
                duck, {"k": f"cast('{value}' as timestamp_ns)"}, [duck.normalize_key(col)]
            )
            assert got == text
            renderings[value] = got
        micro = Column("k", LogicalType.TIMESTAMP, "TIMESTAMP")
        (plain,) = _select(
            duck,
            {"k": "cast('2024-01-01 00:00:00.123456' as timestamp)"},
            [duck.normalize_key(micro)],
        )
        assert plain == renderings["2024-01-01 00:00:00.1234560"]
    finally:
        duck.close()
    assert len(set(renderings.values())) == len(NS_KEYS)
    if not MSSQL_URL:
        return
    ms = get_dialect(MSSQL_URL, side="B")
    try:
        for raw, cast_to, offset in (
            ("datetime2(7)", "datetime2(7)", ""),
            ("datetimeoffset(7)", "datetimeoffset(7)", " +00:00"),
        ):
            col = Column("k", LogicalType.TIMESTAMP, raw)
            for value, text in NS_KEYS:
                (got,) = _select(
                    ms,
                    {"k": f"cast('{value}{offset}' as {cast_to})"},
                    [ms.normalize_key(col)],
                )
                assert got == text, (raw, value)
    finally:
        ms.close()


# ------------------------------------------------- end to end, planted


def _duck_table(path: str, ddl: str, rows: list[tuple]) -> str:
    """A DuckDB file holding table `t` with these rows; returns its path."""
    con = duckdb_write(path)
    try:
        con.execute(f"create table t ({ddl})")
        for r in rows:
            con.execute(f"insert into t values ({', '.join('?' * len(r))})", list(r))
    finally:
        con.close()
    return path


def _diff(tmp_path, ddl, rows_a, rows_b, key, ddl_b=None):
    """Diff two freshly built DuckDB tables."""
    pytest.importorskip("duckdb")
    pa = _duck_table(str(tmp_path / "a.duckdb"), ddl, rows_a)
    pb = _duck_table(str(tmp_path / "b.duckdb"), ddl_b or ddl, rows_b)
    a, b = open_duckdb(pa, side="A"), open_duckdb(pb, side="B")
    try:
        return diff(a, b, "main.t", "main.t", key)
    finally:
        a.close()
        b.close()


def test_backslash_values_are_found_and_reported_unescaped_by_key(tmp_path):
    """Planted: three rows whose only change is a backslash or a chr(31)
    moved or added; every one is found, and nothing else is."""
    rows_a = [(1, "C:" + BS + "x"), (2, f"a{SEP}b"), (3, NULL_SENTINEL), (4, "same" + BS)]
    rows_b = [(1, "C:" + BS + BS + "x"), (2, f"a{BS}sb"), (3, None), (4, "same" + BS)]
    result = _diff(tmp_path, "id int, v varchar", rows_a, rows_b, "id")
    assert sorted((d.key, d.kind) for d in result.diffs) == [
        (1, "different"),
        (2, "different"),
        (3, "different"),
    ]
    by_key = {d.key: d for d in result.diffs}
    assert decode_canonical_field(by_key[2].values_a["v"]) == f"a{SEP}b"
    assert decode_canonical_field(by_key[3].values_b["v"]) is None


def test_text_key_with_escapes_matches_rows_exactly(tmp_path):
    """A text key holding the escape characters matches itself across sides,
    and the planted change on one of them is found under that key."""
    keys = ["k" + BS, f"k{SEP}", NULL_SENTINEL, "k" + BS + "s", "plain"]
    rows_a = [(k, i) for i, k in enumerate(keys)]
    rows_b = [(k, i if k != NULL_SENTINEL else 99) for i, k in enumerate(keys)]
    result = _diff(tmp_path, "k varchar, v int", rows_a, rows_b, "k")
    assert [(decode_canonical_field(str(d.key)), d.kind) for d in result.diffs] == [
        (NULL_SENTINEL, "different")
    ]


def test_decimal_keys_of_different_declared_scales_match(tmp_path):
    """numeric(10,2) on one side and decimal(18,8) on the other hold the same
    keys; they match, and the one planted change is found under its key."""
    rows = [(Decimal("1.5"), 1), (Decimal("2.25"), 2), (Decimal("3"), 3)]
    rows_b = [(Decimal("1.5"), 1), (Decimal("2.25"), 20), (Decimal("3"), 3)]
    result = _diff(
        tmp_path,
        "k decimal(10,2), v int",
        rows,
        rows_b,
        "k",
        ddl_b="k decimal(18,8), v int",
    )
    assert [(d.key, d.kind) for d in result.diffs] == [("2.25", "different")]


def test_decimal_key_differing_past_the_float_scale_is_two_rows(tmp_path):
    """Planted: keys 1.00000001 and 1.00000002 are different rows (RT-A18)."""
    result = _diff(
        tmp_path,
        "k decimal(18,8), v int",
        [(Decimal("1.00000001"), 1)],
        [(Decimal("1.00000002"), 1)],
        "k",
    )
    assert sorted((d.key, d.kind) for d in result.diffs) == [
        ("1.00000001", "only_in_a"),
        ("1.00000002", "only_in_b"),
    ]


def test_null_hashed_key_is_refused_naming_side_and_table(tmp_path):
    """RT-B2: a NULL text key is refused like a NULL integer key."""
    with pytest.raises(ValueError, match=r"\[side A\].*main\.t.*1 NULL"):
        _diff(tmp_path, "k varchar, v int", [(None, 1), ("x", 2)], [("x", 2)], "k")


def test_integral_decimal_key_matches_an_integer_key(tmp_path):
    """A bigint key migrated to decimal(18,0) renders `3` on both sides, so
    rows match (RT-B8, RT-E6 rendered `3.000000` and matched nothing); the
    one planted change is found under its key."""
    rows = [(i, i * 10) for i in range(1, 6)]
    rows_b = [(Decimal(i), 99 if i == 3 else i * 10) for i in range(1, 6)]
    result = _diff(
        tmp_path, "k bigint, v int", rows, rows_b, "k", ddl_b="k decimal(18,0), v int"
    )
    assert [(d.key, d.kind) for d in result.diffs] == [("3", "different")]
