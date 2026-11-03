"""PostgreSQL session hardening, date edge values and connection-error hygiene.

Each finding here was a red-team result (tests/redteam, RT-*); this file pins
the mechanism of each fix, with a planted difference wherever a verdict is
involved (CLAUDE.md section 8). Values are rendered from inline literals, so
no table is created and the PostgreSQL tests only read.
"""

from __future__ import annotations

import pytest
from conftest import open_pg

from parity.dialects.base import get_dialect
from parity.dialects.postgres_dialect import (
    DATE_INFINITY,
    DATE_NEG_INFINITY,
    PostgresDialect,
    connection_secrets,
    redact,
)
from parity.types import Column, KeySpec, LogicalType

SECRET = "Sup3rS3cret"

# ------------------------------------------------------------------ offline


@pytest.mark.parametrize(
    "conn",
    [
        f"postgres://u:{SECRET}@h:5432/db",
        f"postgres://u:{SECRET} x@h/db",
        f"postgres://u:{SECRET}%zz@h/db",
        f"postgresql://u:{SECRET}@[::1:5432/db",
        f"postgres://u@h/db?password={SECRET} x",
        f"postgres://u@h/db?sslmode=require&password={SECRET}&x=1",
        f"host=h password={SECRET} user=u",
        f"host=h password='{SECRET} y' user=u",
    ],
)
def test_every_spelling_of_a_password_is_found(conn):
    """URI userinfo, URI query and key/value forms: each password is found."""
    assert any(SECRET in s for s in connection_secrets(conn)), connection_secrets(conn)


@pytest.mark.parametrize(
    ("conn", "message"),
    [
        (
            f"Postgres://u:{SECRET}@h/db",
            f'missing "=" after "Postgres://u:{SECRET}@h/db" in connection info string',
        ),
        (
            f"postgres://u:{SECRET} x@h/db",
            f'unexpected spaces found in "{SECRET} x", use percent-encoded spaces',
        ),
        (
            f"postgres://u:{SECRET}%zz@h/db",
            f'invalid percent-encoded token: "{SECRET}%zz"',
        ),
        # libpq quoting only the piece of the password before a space.
        (f"postgres://u:{SECRET} x@h/db", f'invalid token "{SECRET[:6]}"'),
        (f"host=h password={SECRET} user=u", f'bad value "{SECRET}" and {SECRET}'),
    ],
)
def test_redaction_removes_the_password_and_any_quoted_piece_of_it(conn, message):
    """RT-F2: what libpq echoes back loses the password, and only that."""
    clean = redact(message, connection_secrets(conn))
    assert SECRET[:6] not in clean
    # Only the secret goes: the rest of libpq's explanation stays readable.
    assert clean.split(" ")[0] == message.split(" ")[0]


def test_redaction_keeps_what_is_not_secret():
    """Over-redacting a quoted option name would make the error useless."""
    msg = 'invalid URI query parameter: "bogus"'
    assert redact(msg, connection_secrets(f"postgres://u:{SECRET}@h/db?bogus=1")) == msg


def test_an_upper_case_scheme_reaches_libpq_lower_case(monkeypatch):
    """libpq reads `Postgres://...` as a key/value string and echoes it whole."""
    psycopg = pytest.importorskip("psycopg")
    seen: list[str] = []

    def fake_connect(conninfo, **kwargs):
        """Record what libpq would be given, then fail before the network."""
        seen.append(conninfo)
        raise psycopg.OperationalError("stop before the network")

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    with pytest.raises(ValueError, match=r"\[side A: postgres\] could not connect"):
        get_dialect(f"POSTGRESQL://u:{SECRET}@h/db", side="A")
    assert seen == [f"postgresql://u:{SECRET}@h/db"]


def test_a_text_key_is_counted_by_its_bytes_and_an_integer_key_unchanged():
    """RT-B10 / RT-D11: a nondeterministic collation must not merge keys."""
    d = PostgresDialect(side="A")
    text_key = KeySpec((Column("k", LogicalType.STRING, "text"),), True)
    int_key = KeySpec((Column("id", LogicalType.INTEGER, "integer"),), False)
    assert 'collate pg_catalog."C"' in d.count_distinct_key(text_key, "x")
    assert d.count_distinct_key(int_key, '"id"') == 'count(distinct "id")'


def test_an_ad_date_renders_through_the_same_to_char_as_before():
    """The AD branch is the old expression verbatim, so no checksum moves."""
    d = PostgresDialect()
    date = d.normalize(Column("d", LogicalType.DATE, "date"))
    ts = d.normalize(Column("t", LogicalType.TIMESTAMP, "timestamp without time zone"))
    assert "else to_char(\"d\", 'YYYY-MM-DD') end" in date
    assert "else to_char(\"t\", 'YYYY-MM-DD HH24:MI:SS.US') end" in ts


# ------------------------------------------------------------------- live

#: (PostgreSQL literal, DuckDB literal) of one value. DuckDB spells BC `(BC)`.
DATE_VALUES = [
    ("infinity", "infinity"),
    ("-infinity", "-infinity"),
    ("4713-01-01 BC", "4713-01-01 (BC)"),
    ("1000-06-30 BC", "1000-06-30 (BC)"),
    ("0044-03-15 BC", "0044-03-15 (BC)"),
    ("0005-01-01 BC", "0005-01-01 (BC)"),
    ("0001-12-31 BC", "0001-12-31 (BC)"),
    ("0001-01-01 BC", "0001-01-01 (BC)"),
    ("0001-01-01", "0001-01-01"),
    ("0044-03-15", "0044-03-15"),
    ("2024-02-29", "2024-02-29"),
    ("9999-12-31", "9999-12-31"),
    ("12345-01-01", "12345-01-01"),
]

#: (type on PostgreSQL, type on DuckDB, time suffix appended to finite values)
DATE_TYPES = [
    ("date", "date", ""),
    ("timestamp", "timestamp", " 01:02:03.456789"),
    ("timestamptz", "timestamptz", " 23:30:00.5+00"),
]


def _render(dialect, typ: str, literal: str) -> str:
    """Canonical text of one literal of `typ`, as `dialect` renders it."""
    lt = LogicalType.DATE if typ == "date" else LogicalType.TIMESTAMP
    expr = dialect.normalize(Column("v", lt, typ))
    return dialect.query(
        f"select {expr} from (select cast('{literal}' as {typ}) as v) s"
    )[0][0]


@pytest.fixture(scope="module")
def pg_dialect(pg_url):
    """One read-only PostgreSQL side for the whole module."""
    d = open_pg(pg_url, side="A")
    yield d
    d.close()


@pytest.fixture(scope="module")
def duck_dialect():
    """An in-memory DuckDB side, the reference for the date rendering."""
    pytest.importorskip("duckdb")
    d = get_dialect("duckdb:///:memory:", side="B")
    yield d
    d.close()


def _literal(value: str, suffix: str) -> str:
    """`value` with a time part added, the era where each engine wants it."""
    if "infinity" in value or not suffix:
        return value
    # PostgreSQL takes the era after the time, DuckDB only after the date.
    if value.endswith(" BC"):
        return value[:-3] + suffix + " BC"
    return value.replace(" (BC)", " (BC)" + suffix) if "(BC)" in value else value + suffix


@pytest.mark.postgres
@pytest.mark.duckdb
@pytest.mark.parametrize(("pg_t", "duck_t", "suffix"), DATE_TYPES, ids=lambda x: x)
def test_edge_dates_render_identically_on_postgres_and_duckdb(
    pg_dialect, duck_dialect, pg_t, duck_t, suffix
):
    """RT-A5/A6/E3b/E4b: infinities and BC dates agree byte for byte."""
    for pg_v, duck_v in DATE_VALUES:
        a = _render(pg_dialect, pg_t, _literal(pg_v, suffix))
        b = _render(duck_dialect, duck_t, _literal(duck_v, suffix))
        assert a == b, f"{pg_t} {pg_v}: postgres {a!r}, duckdb {b!r}"


@pytest.mark.postgres
@pytest.mark.parametrize(("pg_t", "duck_t", "suffix"), DATE_TYPES, ids=lambda x: x)
def test_edge_dates_stay_distinct_from_each_other_and_from_null(
    pg_dialect, pg_t, duck_t, suffix
):
    """The planted half: every value above is its own text, none is NULL's,
    and 44 BC is not AD 44 - the false matches RT-A5/A6/B11/C9/C10 found."""
    texts = [_render(pg_dialect, pg_t, _literal(v, suffix)) for v, _ in DATE_VALUES]
    assert len(set(texts)) == len(texts), texts
    assert pg_dialect.null_sentinel_sql().strip("'") not in texts
    assert texts[0] == DATE_INFINITY and texts[1] == DATE_NEG_INFINITY


@pytest.mark.postgres
def test_ad_dates_render_exactly_as_the_old_to_char(pg_dialect):
    """Ordinary data's checksums do not move: the new expression equals the
    bare `to_char` it replaced on every AD value, timestamptz included."""
    for typ, fmt, value in [
        ("date", "YYYY-MM-DD", "0001-01-01"),
        ("date", "YYYY-MM-DD", "2024-02-29"),
        ("timestamp", "YYYY-MM-DD HH24:MI:SS.US", "1970-01-01 00:00:00"),
        ("timestamp", "YYYY-MM-DD HH24:MI:SS.US", "2024-02-29 13:04:05.123456"),
        ("timestamptz", "YYYY-MM-DD HH24:MI:SS.US", "2024-06-15 12:00:00+02"),
    ]:
        old = pg_dialect.query(f"select to_char(cast('{value}' as {typ}), '{fmt}')")[0][0]
        assert _render(pg_dialect, typ, value) == old


#: Settings a role or URL default might change, set to non-default values.
HOSTILE = {
    "standard_conforming_strings": "off",
    "IntervalStyle": "iso_8601",
    "DateStyle": "SQL,DMY",
    "bytea_output": "escape",
    "client_encoding": "SQL_ASCII",
    "idle_in_transaction_session_timeout": "1000",
    "search_path": "pg_temp,public",
    "TimeZone": "Asia/Tokyo",
    "extra_float_digits": "0",
}


@pytest.mark.postgres
def test_session_pins_override_hostile_defaults(pg_url):
    """RT-D1/D5/D8/D13/D14: every pin wins over a backend-start default."""
    from urllib.parse import quote

    opts = " ".join(f"-c {k}={v}" for k, v in HOSTILE.items())
    d = open_pg(f"{pg_url}?options={quote(opts, safe='')}")
    try:
        got = {k: d.query(f"show {k}")[0][0] for k in HOSTILE}
        assert got == {
            "standard_conforming_strings": "on",
            "IntervalStyle": "postgres",
            "DateStyle": "ISO, MDY",
            "bytea_output": "hex",
            "client_encoding": "UTF8",
            "idle_in_transaction_session_timeout": "0",
            "search_path": "pg_catalog",
            "TimeZone": "UTC",
            "extra_float_digits": "1",
        }
        assert d.query("show lc_monetary")[0][0] == "C"
        if int(d.query("show server_version_num")[0][0]) >= 170000:
            assert d.query("show transaction_timeout")[0][0] == "0"
        # The NULL sentinel literal still means backslash-N.
        assert d.query(f"select {d.null_sentinel_sql()}")[0][0] == "\\N"
    finally:
        d.close()


@pytest.mark.postgres
def test_a_bad_password_error_names_the_side_and_not_the_password(pg_url):
    """A real round trip: authentication fails, nothing secret is printed."""
    url = pg_url.replace("parity:parity@", f"parity:{SECRET}@", 1)
    assert SECRET in url
    with pytest.raises(ValueError) as info:
        get_dialect(url.replace("postgres://", "Postgres://", 1), side="B")
    assert "side B" in str(info.value)
    assert SECRET not in str(info.value)
