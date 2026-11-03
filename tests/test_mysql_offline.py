"""MySQL dialect behaviour that needs no server.

URL parsing (passwords never leak, options are honoured or refused), the
string literal for names, the session statements, the binary/bit/spatial
renderings and the non-transactional-engine caveat. The live counterparts,
each with a planted difference, are in `test_mysql.py` and `tests/redteam`.
"""

from __future__ import annotations

from typing import Any

import pytest

from parity.dialects.mysql_dialect import (
    SQL_MODE,
    MySQLDialect,
    parse_mysql_url,
)
from parity.types import Column, LogicalType

SECRET = "Zq9-s3cret"


# ------------------------------------------------------------------ URL


@pytest.mark.parametrize(
    ("url", "password"),
    [
        (f"mysql://u:{SECRET}/x@h:3306/db", f"{SECRET}/x"),
        (f"mysql://u:{SECRET}#x@h:3306/db", f"{SECRET}#x"),
        (f"mysql://u:{SECRET}?x@h:3306/db", f"{SECRET}?x"),
        (f"mysql://u:{SECRET}%40x@h:3306/db", f"{SECRET}@x"),
    ],
)
def test_an_unencoded_password_is_read_whole(url, password):
    """`/`, `#` and `?` end urlparse's authority early; here they are just
    password characters, and the host, port and database still parse."""
    got = parse_mysql_url(url)
    assert (got.user, got.password, got.host, got.port, got.database) == (
        "u",
        password,
        "h",
        3306,
        "db",
    )
    assert SECRET not in repr(got)


@pytest.mark.parametrize(
    "url",
    [
        f"mysql://u:{SECRET}@h:99999/db",
        f"mysql://u:{SECRET}@h:0/db",
        f"mysql://u:{SECRET}@h:abc/db",
        f"mysql://u:{SECRET}@[::1/db",
        f"mysql://u:{SECRET}@h/db?nope=1",
        f"mysql://u:{SECRET}@h/db?ssl_mode=sometimes",
        f"mysql://u:{SECRET}@h/db?ssl_verify_cert=maybe",
        f"mysql://u:{SECRET}@h/db?ssl_ca=a&ssl_ca=b",
        f"mysql://u:{SECRET}@h/db?ssl_mode=DISABLED&ssl_ca=/ca.pem",
        f"mysql://u:{SECRET}@h/db?ssl_mode=REQUIRED&ssl_disabled=true",
    ],
)
def test_a_bad_url_is_refused_naming_the_side_never_the_password(url):
    """RT-F3/F18: a URL the parser cannot honour fails naming the side, and never echoes the password."""
    with pytest.raises(ValueError) as exc:
        MySQLDialect(side="B").connect(url)
    msg = str(exc.value)
    assert "side B" in msg
    assert SECRET not in msg


def test_ipv6_host_and_defaults():
    """A bracketed IPv6 host loses its brackets; a bare host gets the default port."""
    got = parse_mysql_url("mysql://u@[::1]/db")
    assert (got.host, got.port, got.password) == ("::1", 3306, None)
    got = parse_mysql_url("mysql://h")
    assert (got.host, got.user, got.database) == ("h", None, None)


@pytest.mark.parametrize(
    ("query", "options", "require_tls"),
    [
        ("", {}, False),
        ("ssl_mode=PREFERRED", {}, False),
        ("ssl_mode=disabled", {"ssl_disabled": True}, False),
        ("ssl_mode=REQUIRED", {"ssl_disabled": False}, True),
        (
            "ssl_mode=VERIFY_CA&ssl_ca=C:/certs/ca.pem",
            {"ssl_ca": "C:/certs/ca.pem", "ssl_disabled": False, "ssl_verify_cert": True},
            True,
        ),
        (
            "ssl_mode=VERIFY_IDENTITY",
            {"ssl_disabled": False, "ssl_verify_cert": True, "ssl_verify_identity": True},
            True,
        ),
        (
            "ssl_cert=/c.pem&ssl_key=/k.pem&ssl_verify_cert=1",
            {"ssl_cert": "/c.pem", "ssl_key": "/k.pem", "ssl_verify_cert": True},
            False,
        ),
    ],
)
def test_tls_options_reach_the_driver(query, options, require_tls):
    """RT-F18: each TLS option becomes the mysql-connector argument it names."""
    got = parse_mysql_url(f"mysql://u:p@h/db?{query}" if query else "mysql://u:p@h/db")
    assert got.options == options
    assert got.require_tls is require_tls


class _FakeCursor:
    def __init__(self, log: list[str], cipher: str) -> None:
        """Record into the shared log."""
        self.log, self.cipher, self.rows = log, cipher, []

    def execute(self, sql: str) -> None:
        """Log the statement; a SHOW answers with the configured cipher."""
        self.log.append(sql)
        self.rows = [("Ssl_cipher", self.cipher)] if sql.startswith("show") else []

    def fetchall(self) -> list[Any]:
        """Rows of the last statement."""
        return self.rows

    def close(self) -> None:
        """Nothing to release."""


class _FakeConn:
    def __init__(self, log: list[str], cipher: str) -> None:
        """Record into the shared log."""
        self.log, self.cipher, self.autocommit, self.closed = log, cipher, True, False

    def cursor(self) -> _FakeCursor:
        """A cursor logging into the shared list."""
        return _FakeCursor(self.log, self.cipher)

    def close(self) -> None:
        """Nothing to release."""
        self.closed = True


def _connect(monkeypatch, url: str, cipher: str = "") -> tuple[MySQLDialect, dict, list]:
    """Connect a dialect against a fake driver; return it, the driver kwargs and the SQL log."""
    mc = pytest.importorskip("mysql.connector")
    seen: dict[str, Any] = {}
    log: list[str] = []
    conn = _FakeConn(log, cipher)

    def fake_connect(**kwargs: Any) -> _FakeConn:
        """Capture the driver arguments instead of opening a socket."""
        seen.update(kwargs)
        return conn

    monkeypatch.setattr(mc, "connect", fake_connect)
    d = MySQLDialect(side="A")
    d.connect(url)
    return d, seen, log


def test_the_session_is_pinned_read_only_before_autocommit_goes_off(monkeypatch):
    """RT-F11, RT-D10: zone, sql_mode and READ ONLY are set while autocommit
    is still on, so they bind the first transaction the diff opens."""
    d, seen, log = _connect(monkeypatch, "mysql://u:p@h:3307/db?ssl_ca=/ca.pem")
    assert seen["autocommit"] is True and seen["ssl_ca"] == "/ca.pem"
    assert seen["port"] == 3307 and seen["password"] == "p"
    assert log == [
        "set session time_zone = '+00:00'",
        f"set session sql_mode = '{SQL_MODE}'",
        "set session transaction isolation level repeatable read, read only",
    ]
    assert d._conn.autocommit is False
    assert "PAD_CHAR_TO_FULL_LENGTH" not in SQL_MODE
    assert "NO_BACKSLASH_ESCAPES" not in SQL_MODE  # `string_literal` relies on it


def test_ssl_mode_required_refuses_a_plaintext_session(monkeypatch):
    """The pure-Python connector continues in plaintext when the server has no
    TLS and nothing is verified; REQUIRED must not."""
    with pytest.raises(ValueError, match=r"side A.*not encrypted"):
        _connect(monkeypatch, "mysql://u:p@h/db?ssl_mode=REQUIRED", cipher="")
    d, _, log = _connect(
        monkeypatch, "mysql://u:p@h/db?ssl_mode=REQUIRED", cipher="TLS_AES_256_GCM_SHA384"
    )
    assert log[-1] == "show session status like 'Ssl_cipher'"


# ------------------------------------------------------------- literals


@pytest.mark.parametrize(
    ("name", "literal"),
    [
        ("orders", "'orders'"),
        ("o'k", "'o''k'"),
        ("a\\b", "'a\\\\b'"),
        ("z\\", "'z\\\\'"),
        ("\\'", "'\\\\'''"),
    ],
)
def test_string_literal_escapes_backslashes(name, literal):
    """RT-F17: MySQL reads `\\` in a literal as an escape (sql_mode pinned)."""
    assert MySQLDialect().string_literal(name) == literal


# ------------------------------------------------------------ rendering


def _render(raw: str) -> str:
    """The canonical-text SQL for a column `v` of this raw MySQL type."""
    return MySQLDialect().normalize(Column("v", LogicalType.UNKNOWN, raw))


@pytest.mark.parametrize("raw", ["binary", "varbinary", "blob", "longblob", "tinyblob"])
def test_binary_renders_as_bytea_style_hex(raw):
    """RT-A8/C4: bytes render as backslash-x hex, never through a UTF-8 cast that yields NULL."""
    sql = _render(raw)
    assert "lower(hex(`v`))" in sql
    assert "convert(0x5c78 using utf8mb4)" in sql  # the `\x` prefix
    assert "cast(`v` as char)" not in sql
    assert "uuid_to_bin" in sql  # NULL from a too-long hex raises


@pytest.mark.parametrize("raw", ["geometry", "point", "polygon", "geomcollection"])
def test_spatial_renders_its_stored_bytes(raw):
    """RT-C4: geometry is its stored SRID+WKB bytes as hex."""
    sql = _render(raw)
    assert "lower(hex(`v`))" in sql and "0x5c78" not in sql


def test_bit_renders_its_bits_at_full_width():
    """RT-A8/C4: BIT(n) renders its n digits, leading zeros kept."""
    assert "lpad(bin(`v` + 0), 12, '0')" in _render("bit(12)")
    assert "bin(`v` + 0)" in _render("bit")


def test_ordinary_text_renders_as_before():
    """No checksum moves for text: the fallback branch is untouched."""
    assert _render("json") == "coalesce(cast(`v` as char), convert(0x5c4e using utf8mb4))"


def test_timestamp_is_an_instant_and_datetime_is_not():
    """RT-A20/C12: TIMESTAMP is tz-aware, DATETIME naive; BIT keeps its width."""
    from parity.dialects.mysql_dialect import _column

    assert _column("t", "timestamp", None).tz_aware is True
    assert _column("t", "datetime", None).tz_aware is False
    assert _column("t", "date", None).tz_aware is None
    assert _column("b", "bit", 8).raw_type == "bit(8)"
    assert _column("b", "varbinary", None).logical_type is LogicalType.UNKNOWN


def test_row_hash_falls_back_rather_than_going_null():
    """RT-A17: a row too wide for concat_ws hashes per-column md5s instead."""
    d = MySQLDialect()
    cols = [Column("x", LogicalType.STRING, "longtext"), Column("y", LogicalType.STRING)]
    sql = d.row_hash(cols)
    plain = d.hash_expr(d.row_text(cols))
    assert sql.startswith(f"coalesce({plain}, ")
    assert "'parity: wide row'" in sql
    assert d.row_hash([]) == d.hash_expr(d.row_text([]))


def test_a_non_transactional_table_is_a_caveat():
    """RT-D3: a MyISAM table reaches the output as a no-snapshot caveat."""
    d = MySQLDialect(side="A")
    assert d.caveats([]) == []
    d._no_snapshot = {"legacy": "MyISAM"}
    (note,) = d.caveats([])
    assert "legacy" in note and "MyISAM" in note and "snapshot" in note
