"""The Oracle dialect without a server: URL parsing, refusals, SQL shape.

Everything here runs in CI. The live proof - every expression below run
against Oracle 23ai and checked against DuckDB and the rules - is
`tests/test_oracle.py` and the conformance sweep.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import re

import pytest

from parity.dialects.base import get_dialect
from parity.dialects.oracle_dialect import (
    EMPTY_HASH,
    PARITY_MD5,
    SESSION_PINS,
    OracleDialect,
    _column,
    is_single_select,
    map_type_oracle,
    parse_oracle_url,
)
from parity.types import Column, KeySpec, LogicalType

SECRET = "s3cr;et}pw"
URL = f"oracle://scott:{SECRET.replace(';', '%3B').replace('}', '%7D')}@db.example:1522/ORCLPDB1"


def d(scale: int = 6) -> OracleDialect:
    """An unconnected dialect, for SQL generation."""
    return OracleDialect(float_scale=scale, side="A")


# ------------------------------------------------------------------- URLs


def test_a_url_becomes_connect_arguments():
    """User, unquoted password, host, port and the service name."""
    args = parse_oracle_url(URL)
    assert args == {
        "user": "scott",
        "password": SECRET,
        "host": "db.example",
        "port": 1522,
        "service_name": "ORCLPDB1",
    }


def test_the_port_defaults_to_1521_and_options_are_typed():
    """`tcp_connect_timeout` is seconds, `protocol` only tcp or tcps."""
    args = parse_oracle_url("oracle://u:p@h/SVC?tcp_connect_timeout=2.5&protocol=TCPS")
    assert args["port"] == 1521 and args["tcp_connect_timeout"] == 2.5
    assert args["protocol"] == "tcps"


@pytest.mark.parametrize(
    ("url", "match"),
    [
        ("oracle:///SVC", "needs a host"),
        ("oracle://u:p@h", "exactly one service name"),
        ("oracle://u:p@h/a/b", "exactly one service name"),
        ("oracle://h/SVC", "needs a user"),
        (
            "oracle://u:p@h/SVC?mode=sysdba",
            r"unknown oracle connection parameter\(s\) \['mode'\]",
        ),
        ("oracle://u:p@h/SVC?tcp_connect_timeout=soon", "number of seconds"),
        ("oracle://u:p@h/SVC?protocol=ipc", "tcp or tcps"),
        ("oracle://u:p@h:99999/SVC", "invalid port"),
    ],
)
def test_bad_urls_are_refused(url, match):
    """Every malformed or unknown piece is refused rather than ignored."""
    with pytest.raises(ValueError, match=match):
        parse_oracle_url(url)


def test_a_refused_url_names_the_side_and_never_the_password():
    """The error goes through `get_dialect`, which must name side B and must
    not echo the password - it lands in CI logs."""
    bad = URL + "?wallet=x"
    with pytest.raises(ValueError) as exc:
        get_dialect(bad, side="B")
    assert "[side B: oracle]" in str(exc.value)
    assert SECRET not in str(exc.value) and "s3cr" not in str(exc.value)


def test_the_scheme_is_registered():
    """`oracle://` selects this dialect (it fails at the URL, before any network)."""
    with pytest.raises(ValueError, match="side A: oracle"):
        get_dialect("oracle://u:p@h/SVC?nope=1", side="A")


# ------------------------------------------------------- read-only guard


@pytest.mark.parametrize(
    "sql",
    [
        "select 1 from dual",
        'select count(*) from "S"."T" where "x" = \'delete; drop\'',
        "select to_char(c, 'TM9', 'NLS_NUMERIC_CHARACTERS=''.,''') from t",
    ],
)
def test_generated_selects_pass_the_guard(sql):
    """Words inside identifiers and literals are data, not statements."""
    assert is_single_select(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "delete from t",
        "select * from t for update",
        "select 1 from dual; drop table t",
        "select 1 from dual -- comment",
        "select /* hint */ 1 from dual",
        "select 1 from t where 'unterminated",
        "with function f return number is begin delete from t; return 1; end; select f from dual",
        "select * into x from t",
        "begin null; end;",
        'select 1 from "bad',
    ],
)
def test_anything_but_one_select_is_refused(sql):
    """No second statement, comment, write, lock or PL/SQL block."""
    assert not is_single_select(sql)


def test_query_refuses_before_touching_the_connection():
    """The guard runs first: there is no connection here to touch."""
    with pytest.raises(ValueError, match=r"side A: oracle.*single SELECT"):
        d().query("update t set x = 1")


class FakeCursor:
    """Records the SQL; raises `error` if set."""

    def __init__(self, conn: FakeConn) -> None:
        """Remember the fake connection whose log this cursor writes."""
        self.conn = conn

    def execute(self, sql: str) -> None:
        """Log the SQL, then raise the connection's error if it has one."""
        self.conn.seen.append(sql)
        if self.conn.error:
            raise self.conn.error

    def fetchall(self) -> list[tuple]:
        """One row, whatever the query."""
        return [(1,)]

    def close(self) -> None:
        """Nothing to release."""


class FakeConn:
    """A stand-in python-oracledb connection."""

    def __init__(self, error: Exception | None = None) -> None:
        """An empty SQL log, and the error every execute should raise."""
        self.seen: list[str] = []
        self.error = error

    def cursor(self) -> FakeCursor:
        """A cursor logging into this connection."""
        return FakeCursor(self)


def test_inline_functions_are_declared_only_where_called():
    """`query` prepends `parity_md5` only to SQL that calls it."""
    a = d()
    a._conn = FakeConn()
    a.query("select 1 from dual")
    a.query("select parity_md5(to_clob('a')) from dual")
    assert a._conn.seen[0] == "select 1 from dual"
    assert a._conn.seen[1].startswith("with function parity_md5(t clob)")
    assert a._conn.seen[1].endswith("select parity_md5(to_clob('a')) from dual")


def test_a_refusal_raised_in_sql_names_the_side():
    """ORA-20001 from `parity_refuse` becomes an error naming the side."""
    a = OracleDialect(side="B")
    a._conn = FakeConn(
        RuntimeError(
            "ORA-20001: parity: FLOAT value x in column 'C'\nORA-06512: at line 3"
        )
    )
    with pytest.raises(
        ValueError, match=r"^\[side B: oracle\] parity: FLOAT value x in column 'C'$"
    ):
        a.query("select 1 from dual")


def test_a_snapshot_too_old_for_the_table_is_explained():
    """ORA-01466 says what happened and what to do."""
    a = d()
    a._conn = FakeConn(
        RuntimeError("ORA-01466: unable to read data - table definition has changed")
    )
    with pytest.raises(
        ValueError, match=r"side A: oracle.*definition changed.*Run the diff again"
    ):
        a.query("select 1 from dual")


def test_the_session_pins_cover_every_rendering_setting():
    """Each setting that changes a rendering or a comparison is pinned."""
    pinned = " ".join(SESSION_PINS).lower()
    for name in (
        "nls_numeric_characters", "nls_calendar", "nls_date_format",
        "nls_timestamp_format", "nls_timestamp_tz_format", "nls_sort", "nls_comp",
        "time_zone", "nls_territory", "nls_language", "nls_nchar_conv_excp",
        "cursor_sharing", "query_rewrite_enabled",
    ):  # fmt: skip
        assert name in pinned, name
    # Territory and language reset the formats, so they come first.
    assert SESSION_PINS[0].lower().find("nls_language") > 0
    assert "nls_territory" in SESSION_PINS[1].lower()


# ----------------------------------------------------------------- types


@pytest.mark.parametrize(
    ("data_type", "precision", "scale", "logical", "raw"),
    [
        ("NUMBER", 10, 0, LogicalType.INTEGER, "NUMBER(10,0)"),
        ("NUMBER", None, 0, LogicalType.INTEGER, "NUMBER(*,0)"),
        ("NUMBER", 5, -2, LogicalType.INTEGER, "NUMBER(5,-2)"),
        ("NUMBER", 12, 2, LogicalType.DECIMAL, "NUMBER(12,2)"),
        ("NUMBER", None, None, LogicalType.DECIMAL, "NUMBER"),
        ("FLOAT", 126, None, LogicalType.DECIMAL, "FLOAT(126)"),
        ("BINARY_FLOAT", None, None, LogicalType.FLOAT, "BINARY_FLOAT"),
        ("BINARY_DOUBLE", None, None, LogicalType.FLOAT, "BINARY_DOUBLE"),
        ("VARCHAR2", None, None, LogicalType.STRING, "VARCHAR2"),
        ("NCLOB", None, None, LogicalType.STRING, "NCLOB"),
        ("DATE", None, None, LogicalType.TIMESTAMP, "DATE"),
        ("TIMESTAMP(9)", None, 9, LogicalType.TIMESTAMP, "TIMESTAMP(9)"),
        (
            "INTERVAL DAY(3) TO SECOND(9)",
            3,
            9,
            LogicalType.INTERVAL,
            "INTERVAL DAY(3) TO SECOND(9)",
        ),
        (
            "INTERVAL YEAR(2) TO MONTH",
            2,
            0,
            LogicalType.INTERVAL,
            "INTERVAL YEAR(2) TO MONTH",
        ),
        ("RAW", None, None, LogicalType.BINARY, "RAW"),
        ("BLOB", None, None, LogicalType.BINARY, "BLOB"),
        ("BOOLEAN", None, None, LogicalType.BOOLEAN, "BOOLEAN"),
        ("JSON", None, None, LogicalType.UNKNOWN, "JSON"),
        ("LONG", None, None, LogicalType.UNKNOWN, "LONG"),
        ("XMLTYPE", None, None, LogicalType.UNKNOWN, "XMLTYPE"),
    ],
)
def test_catalog_rows_map_onto_logical_types(data_type, precision, scale, logical, raw):
    """Only the scale tells an integer NUMBER from a decimal one, FLOAT(p) is
    a decimal NUMBER (not IEEE), and DATE holds a time of day."""
    c = _column("C", data_type, precision, scale)
    assert (c.logical_type, c.raw_type) == (logical, raw)
    assert map_type_oracle(raw) is logical


def test_instants_and_wall_clocks_are_marked():
    """WITH (LOCAL) TIME ZONE is an instant; DATE and TIMESTAMP are naive."""
    assert _column("C", "TIMESTAMP(6) WITH TIME ZONE", None, 6).tz_aware is True
    assert _column("C", "TIMESTAMP(3) WITH LOCAL TIME ZONE", None, 3).tz_aware is True
    assert _column("C", "TIMESTAMP(6)", None, 6).tz_aware is False
    assert _column("C", "DATE", None, None).tz_aware is False


@pytest.mark.parametrize(
    ("raw", "native"),
    [
        ("NUMBER(18,0)", True),
        ("NUMBER(10,0)", True),
        ("NUMBER(19,0)", False),
        ("NUMBER(*,0)", False),
    ],
)
def test_only_keys_that_fit_64_bits_are_bucketed_natively(raw, native):
    """A wider integer key would push `(k - lo) * n` past NUMBER's 38 digits,
    where it rounds silently; such keys are hashed instead."""
    assert d().bisects_natively(Column("K", LogicalType.INTEGER, raw)) is native


def test_sub_microsecond_columns_are_recognised_and_caveated():
    """TIMESTAMP(7-9) and INTERVAL DAY TO SECOND(7-9) carry the truncation
    caveat; text columns carry the empty-string one."""
    a = d()
    cols = [
        Column("T9", LogicalType.TIMESTAMP, "TIMESTAMP(9)"),
        Column("T6", LogicalType.TIMESTAMP, "TIMESTAMP(6)"),
        Column("DT", LogicalType.TIMESTAMP, "DATE"),
        Column("IV", LogicalType.INTERVAL, "INTERVAL DAY(2) TO SECOND(9)"),
        Column("S", LogicalType.STRING, "VARCHAR2"),
    ]
    assert [a.holds_sub_micro(c) for c in cols[:3]] == [True, False, False]
    text = "\n".join(a.caveats(cols))
    assert "empty string as NULL" in text and "['S']" in text
    assert "['T9', 'IV']" in text


# ------------------------------------------------------------ SQL shape


def test_the_md5_constants_are_the_rfc_table():
    """The K table in `parity_md5` is floor(|sin(i + 1)| * 2^32), computed
    here independently, so a typo cannot hide in 64 numbers."""
    k = [int(abs(math.sin(i + 1)) * 2**32) for i in range(64)]
    inline = re.search(r"ka\(([\d, ]+)\)", PARITY_MD5)
    assert inline is not None
    assert [int(x) for x in inline.group(1).split(",")] == k
    assert EMPTY_HASH == int(hashlib.md5(b"").hexdigest()[:15], 16)


def test_the_hash_is_the_60_bit_prefix_and_empty_text_is_md5_of_nothing():
    """STANDARD_HASH's first 15 hex digits, NULL (Oracle's '') as md5('')."""
    sql = d().hash_expr("x")
    assert "standard_hash(x, 'MD5')" in sql and "1, 15)" in sql
    assert sql.endswith(f"{EMPTY_HASH})")


def test_a_row_of_fixed_width_parts_hashes_in_sql_only():
    """Numbers and dates have static bounds; 2 + 5 columns stay far below
    4,000 bytes, so no CASE and no PL/SQL is emitted."""
    a = d()
    parts = [
        a.normalize(Column(f"C{i}", LogicalType.INTEGER, "NUMBER(10,0)"))
        for i in range(5)
    ]
    sql = a.hash_parts(parts)
    assert "parity_md5" not in sql and "case when 1" not in sql


def test_a_text_row_chooses_its_hash_path_per_row():
    """A VARCHAR2 part has a dynamic bound, so the row picks STANDARD_HASH or
    `parity_md5` by its raw length; a CLOB part is converted with to_char on
    the short path."""
    a = d()
    v = a.normalize(Column("V", LogicalType.STRING, "VARCHAR2"))
    c = a.normalize(Column("M", LogicalType.STRING, "CLOB"))
    sql = a.hash_parts([v, c])
    assert "<= 4000 then" in sql and "parity_md5(" in sql
    assert '2 * nvl(lengthb("V"), 0)' in sql and 'dbms_lob.getlength("M")' in sql
    assert f"to_char({c})" in sql and f"to_clob({v})" in sql


def test_a_part_of_unknown_origin_always_takes_the_clob_path():
    """No recorded bound means no proof the row fits: the slow, exact path."""
    sql = d().hash_parts(["'abc'", "'def'"])
    assert "standard_hash" not in sql and "parity_md5(" in sql


def test_null_is_decided_by_the_column_not_by_the_rendered_text():
    """Oracle's empty text is NULL; coalescing the rendering would turn an
    all-space CHAR into the sentinel."""
    sql = d().normalize(Column("F", LogicalType.STRING, "CHAR"))
    assert sql.startswith("(case when \"F\" is null then (chr(92) || 'N') else")
    assert "coalesce" not in sql


def test_the_boolean_uses_when_not_never_else():
    """CLAUDE.md 4.3: `else 'false'` would turn NULL into FALSE."""
    sql = d().normalize(Column("B", LogicalType.BOOLEAN, "BOOLEAN"))
    assert "when not \"B\" then 'false'" in sql and "else 'false'" not in sql


def test_integer_division_is_exact_and_grouping_repeats_the_expression():
    """NUMBER `/` rounds a quotient at 38-40 digits; the modulo form divides
    an exact multiple. GROUP BY takes no ordinal."""
    a = d()
    assert a.int_div("n", "d") == "(((n) - mod((n), (d))) / (d))"
    assert a.group_by(1, "expr") == "expr"


def test_identifiers_are_quoted_and_tables_always_qualified():
    """Oracle cannot escape `"` inside a quoted identifier: refused."""
    a = d()
    assert a.qualify("T") == '"PARITY"."T"' and a.qualify("S.T") == '"S"."T"'
    with pytest.raises(ValueError, match="cannot be quoted"):
        a.quote('a"b')


@pytest.mark.parametrize("raw", ["LONG", "XMLTYPE", "VECTOR", "BFILE", "SDO_GEOMETRY"])
def test_a_type_without_a_text_form_is_refused_with_the_way_out(raw):
    """Never a guessed rendering: refused, naming the column and --exclude."""
    with pytest.raises(ValueError, match=r"side A: oracle.*'C'.*--exclude"):
        d().normalize(Column("C", LogicalType.UNKNOWN, raw))


@pytest.mark.parametrize("raw", ["CLOB", "NCLOB", "BLOB"])
def test_a_lob_key_is_refused(raw):
    """Keys are matched as VARCHAR2 text; a LOB key is refused loudly."""
    t = LogicalType.BINARY if raw == "BLOB" else LogicalType.STRING
    with pytest.raises(ValueError, match="cannot be used as a key"):
        d().normalize_key(Column("K", t, raw))


def test_a_float_key_is_refused():
    """No rendering of a double is both exact and identical everywhere."""
    with pytest.raises(ValueError, match="FLOAT column 'K' cannot be used as a key"):
        d().normalize_key(Column("K", LogicalType.FLOAT, "BINARY_DOUBLE"))


def test_scales_past_the_limits_are_refused():
    """FLOAT past FLOAT_MAX_SCALE (20) and DECIMAL past 38 places."""
    with pytest.raises(ValueError, match="maximum is 20"):
        d(21).normalize(Column("X", LogicalType.FLOAT, "BINARY_DOUBLE"))
    with pytest.raises(ValueError, match="maximum is 38"):
        d(39).normalize(Column("X", LogicalType.DECIMAL, "NUMBER"))


def test_hashed_key_uniqueness_is_counted_by_digest():
    """Not by text equality, which a column collation could fold."""
    a = d()
    key = KeySpec((Column("K", LogicalType.STRING, "VARCHAR2"),), True)
    assert "standard_hash(ident, 'SHA256')" in a.count_distinct_key(key, "ident")


def test_downloaded_empty_text_is_the_empty_string():
    """Every rendering is non-NULL but the empty one, which Oracle returns as NULL."""
    a = d()
    assert a.fetched(None) == "" and a.fetched("x") == "x"


def test_intervals_from_the_driver_become_their_three_components():
    """timedelta (DAY TO SECOND: one sign throughout) and IntervalYM."""

    class YM:
        years, months = -1, -2

    a = d()
    iv = Column("I", LogicalType.INTERVAL, "INTERVAL DAY(9) TO SECOND(6)")
    assert a.sample_value(iv, dt.timedelta(microseconds=-1)) == (0, 0, -1)
    assert a.sample_value(iv, dt.timedelta(days=-2, seconds=3600)) == (
        0,
        -1,
        -82_800_000_000,
    )
    assert a.sample_value(iv, YM()) == (-14, 0, 0)


def test_long_rows_use_dbms_crypto_only_where_the_login_may():
    """`parity_md5` for a bare reader; DBMS_CRYPTO.HASH where `connect` found
    it callable. Either way a NULL (all-empty) row text hashes as md5('')."""
    a = d()
    assert "parity_md5(x)" in a._clob_hash("x")
    a.crypto = True
    sql = a._clob_hash("x")
    assert "dbms_crypto.hash(x, 2)" in sql and "parity_md5" not in sql
    assert sql.endswith(f"{EMPTY_HASH})")


@pytest.mark.parametrize(
    ("raw", "logical", "bound"),
    [
        ("NUMBER(10,0)", LogicalType.INTEGER, "11"),
        ("NUMBER(5,-2)", LogicalType.INTEGER, "8"),
        ("NUMBER(*,0)", LogicalType.INTEGER, "40"),
        ("NUMBER(12,2)", LogicalType.DECIMAL, "19"),
        ("NUMBER(3,5)", LogicalType.DECIMAL, "10"),
        ("NUMBER", LogicalType.DECIMAL, "47"),
        ("BINARY_DOUBLE", LogicalType.FLOAT, "43"),
    ],
)
def test_static_bounds_cover_the_longest_text(raw, logical, bound):
    """At scale 6: sign, every integer digit, a rounding carry, point, places.
    NUMBER(12,2) at its maximum 9999999999.99 rounds to 10000000000.000000 -
    18 characters, within 19."""
    a = d()
    part = a.normalize(Column("C", logical, raw))
    assert a._parts[part][0] == bound


def test_the_row_bound_counts_the_separators_between_parts():
    """The 4,000-byte bound is the parts' bounds *plus* one chr(31) between
    each pair. NUMBER(10,0) renders in at most 11 bytes, so 333 such parts
    need 11 * 333 + 332 = 3,995 bytes and fit STANDARD_HASH, while 334 need
    4,007 and must not: forgetting the separators (3,674) would send that row
    to STANDARD_HASH and fail it with ORA-01489."""
    a = d()

    def row(n):
        """The hash of a row of `n` NUMBER(10,0) columns."""
        parts = [
            a.normalize(Column(f"C{i}", LogicalType.INTEGER, "NUMBER(10,0)"))
            for i in range(n)
        ]
        assert {a._parts[p][0] for p in parts} == {"11"}
        return a.hash_parts(parts)

    assert "parity_md5(" not in row(333)
    assert "parity_md5(" in row(334)
