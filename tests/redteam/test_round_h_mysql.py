"""Round H red team: MySQL (findings RT-HM<n>).

Every test plants a difference and asserts that exactly it is found, or
asserts a refusal. A confirmed problem is a strict xfail carrying its RT-HM
id, so it turns green the moment it is fixed; the rest are regressions.

Endpoints:

- the shared native MySQL (`PARITY_TEST_MYSQL`, conftest) for data tests;
- the shared native PostgreSQL (`PARITY_TEST_PG`) for cross-engine tests;
- a disposable Docker MySQL 8.0 with root, for server settings and grants:

      docker run -d --name rth-mysql-80 -e MYSQL_ROOT_PASSWORD=rth \\
          -e MYSQL_DATABASE=rth -e MYSQL_USER=rth -e MYSQL_PASSWORD=rth \\
          -p 13480:3306 mysql:8.0 --innodb-buffer-pool-size=32M

- a disposable MariaDB 11 (`rth-mysql-maria11`, port 13411, same users).

Each skips cleanly when its server is not running. The container passwords
are throwaway values for those local containers only. Every table is named
`rth_mysql_*` and dropped by the fixture that made it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from parity.dialects.base import get_dialect

from .rtg_servers import found, mysql_connect, mysql_run, run_diff

RUN = uuid.uuid4().hex[:6]

#: Root and a plain login on the Docker MySQL 8.0 (`rth-mysql-80`).
ROOT80 = os.environ.get("PARITY_RTH_MYSQL_ROOT", "mysql://root:rth@127.0.0.1:13480/rth")
USER80 = os.environ.get("PARITY_RTH_MYSQL_USER", "mysql://rth:rth@127.0.0.1:13480/rth")
#: The plain login on the Docker MariaDB 11 (`rth-mysql-maria11`).
MARIA = os.environ.get("PARITY_RTH_MARIADB", "mysql://rth:rth@127.0.0.1:13411/rth")


# ------------------------------------------------------------------- helpers


@pytest.fixture
def tables() -> Iterator[Callable[..., str]]:
    """`make(url, tag, ddl, *statements)` builds table `rth_mysql_<tag>_<run>`
    on `url` and returns its name; every table made is dropped afterwards.

    A statement is SQL with `{t}` standing for the table, or a pair
    (SQL, params) for the connector to bind.
    """
    made: list[tuple[str, str]] = []

    def make(url: str, tag: str, ddl: str, *statements: Any) -> str:
        name = f"rth_mysql_{tag}_{RUN}"
        conn = mysql_connect(url)
        cur = conn.cursor()
        try:
            cur.execute(f"drop table if exists `{name}`")
            made.append((url, name))
            cur.execute(f"create table `{name}` ({ddl})")
            for s in statements:
                sql, params = s if isinstance(s, tuple) else (s, None)
                cur.execute(sql.replace("{t}", f"`{name}`"), params)
        finally:
            cur.close()
            conn.close()
        return name

    yield make
    for url, name in made:
        conn = mysql_connect(url)
        try:
            mysql_run(conn, f"drop table if exists `{name}`")
        finally:
            conn.close()


def _mysql_pair(
    make: Callable[..., str],
    url: str,
    ddl: str,
    a: list[Any],
    b: list[Any],
    tag: str = "p",
    url_b: str | None = None,
    **kw: Any,
):
    """Build `<tag>_a` and `<tag>_b` and diff them, key `id` by default."""
    ta = make(url, f"{tag}_a", ddl, *a)
    tb = make(url_b or url, f"{tag}_b", ddl, *b)
    return run_diff(
        get_dialect(url, side="A"), get_dialect(url_b or url, side="B"), ta, tb, **kw
    )


@pytest.fixture
def pg_table(pg_url) -> Iterator[Callable[..., str]]:
    """`make(ddl, *statements)` builds a table in schema `rth_mysql` on the
    native PostgreSQL and returns its qualified name; dropped afterwards."""
    psycopg = pytest.importorskip("psycopg")
    conn = psycopg.connect(pg_url, autocommit=True)
    conn.execute("create schema if not exists rth_mysql")
    made: list[str] = []

    def make(tag: str, ddl: str, *statements: str) -> str:
        name = f"rth_mysql.{tag}_{RUN}"
        conn.execute(f"drop table if exists {name}")
        made.append(name)
        conn.execute(f"create table {name} ({ddl})")
        for s in statements:
            conn.execute(s.replace("{t}", name))
        return name

    yield make
    for name in made:
        conn.execute(f"drop table if exists {name}")
    conn.close()


def _cross(make, pg_make, mysql_url, pg_url, my_ddl, my_rows, pg_ddl, pg_rows, **kw):
    """MySQL side A against PostgreSQL side B."""
    ta = make(mysql_url, "x", my_ddl, *my_rows)
    tb = pg_make("x", pg_ddl, *pg_rows)
    return run_diff(
        get_dialect(mysql_url, side="A"), get_dialect(pg_url, side="B"), ta, tb, **kw
    )


def _init_hook(monkeypatch, *statements: str) -> None:
    """Run `statements` on every new MySQL connection before the connector's
    own setup and the dialect's pins - where a server's `init_connect` runs.
    Client-side, so the shared server is untouched (as in RT-G)."""
    from mysql.connector.abstracts import MySQLConnectionAbstract

    real = MySQLConnectionAbstract._post_connection

    def post_connection(self) -> None:
        for s in statements:
            self._execute_query(s)
        real(self)

    monkeypatch.setattr(MySQLConnectionAbstract, "_post_connection", post_connection)


@pytest.fixture
def root80():
    """Root on the Docker MySQL 8.0, or skip."""
    conn = mysql_connect(ROOT80)
    mysql_run(conn, "grant all on rth.* to rth@'%'")
    yield conn
    conn.close()


# ====================================================================
# RT-HM1: a legacy character set whose bytes do not all map to Unicode
# ====================================================================
#
# Every STRING renders through `cast(c as char)`, which converts the column's
# character set to the connection's utf8mb4. MySQL replaces every byte
# sequence with no Unicode mapping by `?` (or U+FFFD) - with no error, in any
# sql_mode - so two different stored values render as the same text. Such
# bytes are ordinary legacy data, not corruption: a client that runs
# `SET NAMES sjis` and writes a vendor or user-defined (gaiji) character
# stores exactly its bytes, because no conversion happens between equal
# character sets. Measured on MySQL 8.0.42 over every 1- and 2-byte value:
# sjis maps 1,448 storable characters onto `?`, euckr 1,792, gbk 712,
# big5 85, hebrew 37, geostd8 42, swe7 130, cp1257 13. The driver converts
# the same way, so the sample check reads `?` on both sides and agrees.

LOSSY = [
    ("sjis", "F040", "F041"),  # two Shift-JIS user-defined characters
    ("euckr", "C7A0", "D367"),
    ("big5", "C867", "C876"),
    ("gbk", "A140", "A161"),
    ("ujis", "A9A9", "ADCD"),
    ("gb2312", "A8FD", "ACBE"),
    ("hebrew", "C3", "C4"),
    ("greek", "AA", "FF"),
    ("cp1250", "81", "83"),
    ("cp1251", "98", "3F"),  # an unassigned byte against a real '?'
    ("cp1256", "8A", "9A"),
    ("cp1257", "A1", "A5"),
    ("tis620", "DC", "DE"),  # both U+FFFD
    ("geostd8", "81", "83"),
    ("swe7", "80", "81"),
    ("dec8", "A4", "A6"),
]


@pytest.mark.xfail(
    strict=True,
    reason="RT-HM1: legacy-charset bytes with no Unicode mapping all render as '?'",
)
@pytest.mark.parametrize(("charset", "va", "vb"), LOSSY, ids=[c[0] for c in LOSSY])
def test_rt_hm1_unmappable_legacy_characters_are_not_one_value(
    tables, mysql_url, charset, va, vb
):
    """Two different stored characters in a legacy-charset column must not
    compare equal. Today they both render `?` and the tables are identical."""
    r = _mysql_pair(
        tables,
        mysql_url,
        f"id int primary key, v varchar(10) character set {charset}",
        [f"insert into {{t}} values (1, _{charset} x'{va}'), (2, 'same')"],
        [f"insert into {{t}} values (1, _{charset} x'{vb}'), (2, 'same')"],
        tag=f"cs_{charset}",
    )
    assert found(r) == [(1, "different", ("v",))]


@pytest.mark.xfail(
    strict=True,
    reason="RT-HM1: an unmappable Shift-JIS character equals a PostgreSQL '?'",
)
def test_rt_hm1_unmappable_sjis_character_against_postgres_question_mark(
    tables, pg_table, mysql_url, pg_url
):
    """Across engines the lost character equals the literal text `?`: a
    migration that replaced every gaiji by `?` diffs as identical."""
    r = _cross(
        tables,
        pg_table,
        mysql_url,
        pg_url,
        "id int primary key, v varchar(10) character set sjis",
        ["insert into {t} values (1, _sjis x'F040'), (2, _sjis x'8341')"],
        "id int primary key, v text",
        ["insert into {t} values (1, '?'), (2, 'ア')"],
    )
    assert found(r) == [(1, "different", ("v",))]


@pytest.mark.parametrize(
    ("charset", "va", "vb"),
    [
        ("latin1", "81", "8D"),  # MySQL's latin1 maps all 256 bytes
        ("koi8r", "C1", "C2"),
        ("cp850", "B0", "B1"),
        ("macroman", "F0", "F1"),
        ("latin2", "A1", "A2"),
        ("cp866", "F0", "F1"),
        ("gb18030", "A140", "A141"),  # maps every code point, PUA included
        ("utf8mb3", "E4B8AD", "E69687"),
        ("utf16", "D83DDE00", "D83DDE01"),  # real pairs in utf16 are fine
        ("utf32", "0001F600", "0001F601"),
        ("ucs2", "4E2D", "6587"),
    ],
)
def test_lossless_character_sets_keep_every_difference(
    tables, mysql_url, charset, va, vb
):
    """The control for RT-HM1: where every stored character has a Unicode
    mapping, the conversion is exact and the planted difference is found."""
    r = _mysql_pair(
        tables,
        mysql_url,
        f"id int primary key, v varchar(10) character set {charset}",
        [f"insert into {{t}} values (1, _{charset} x'{va}'), (2, 'same')"],
        [f"insert into {{t}} values (1, _{charset} x'{vb}'), (2, 'same')"],
        tag=f"ok_{charset}",
    )
    assert found(r) == [(1, "different", ("v",))]


# ====================================================================
# RT-HM2: a surrogate in a ucs2 column makes the whole value NULL
# ====================================================================
#
# ucs2 stores 16-bit units without checking pairs, so an application that
# writes UTF-16 into a ucs2 column (the usual way emoji reach one) stores
# surrogate halves. Converting such a value to utf8mb4 returns SQL NULL - the
# whole value, not one character - so it renders as the NULL sentinel: two
# different emoji are equal, and both equal a real NULL. The driver decodes
# the same bytes to U+FFFD, so the sample check agrees on the emoji pair and
# catches only the NULL case, and only for rows it samples.


@pytest.mark.xfail(strict=True, reason="RT-HM2: ucs2 surrogate pairs render as NULL")
def test_rt_hm2_two_different_emoji_in_ucs2_are_not_equal(tables, mysql_url):
    r = _mysql_pair(
        tables,
        mysql_url,
        "id int primary key, v varchar(10) character set ucs2",
        ["insert into {t} values (1, _ucs2 x'D83DDE00'), (2, _ucs2 x'0041')"],
        ["insert into {t} values (1, _ucs2 x'D83DDE01'), (2, _ucs2 x'0041')"],
        tag="ucs2_emoji",
    )
    assert found(r) == [(1, "different", ("v",))]


@pytest.mark.sample_check_may_disagree
@pytest.mark.xfail(strict=True, reason="RT-HM2: a ucs2 surrogate equals NULL in the walk")
def test_rt_hm2_ucs2_value_with_a_surrogate_is_not_null(tables, mysql_url):
    r = _mysql_pair(
        tables,
        mysql_url,
        "id int primary key, v varchar(10) character set ucs2",
        ["insert into {t} values (1, _ucs2 x'0041D800'), (2, _ucs2 x'0041')"],
        ["insert into {t} values (1, null), (2, _ucs2 x'0041')"],
        tag="ucs2_null",
    )
    assert found(r) == [(1, "different", ("v",))]


# ====================================================================
# RT-HM3: column-level grants hide columns without a word
# ====================================================================


@pytest.mark.xfail(
    strict=True,
    reason="RT-HM3: columns the login cannot read are silently left out",
)
def test_rt_hm3_column_level_grants_do_not_silently_narrow_the_comparison(root80, tables):
    """A login granted SELECT on some columns only sees only those in
    information_schema.columns, so the rest are never compared: with the
    same login on both sides the planted difference in `secret` reads
    identical, and nothing says a column was left out. The tool should refuse
    or at least warn (information_schema.column_privileges shows the grant)."""
    ta = tables(ROOT80, "grant_a", "id int primary key, a varchar(10), secret varchar(10)",
                "insert into {t} values (1, 'x', 'one'), (2, 'y', 'two')")  # fmt: skip
    tb = tables(ROOT80, "grant_b", "id int primary key, a varchar(10), secret varchar(10)",
                "insert into {t} values (1, 'x', 'ONE'), (2, 'y', 'two')")  # fmt: skip
    user = f"rth_col_{RUN}"
    mysql_run(
        root80,
        f"drop user if exists {user}@'%'",
        f"create user {user}@'%' identified by 'rthcol'",
        f"grant select (id, a) on rth.{ta} to {user}@'%'",
        f"grant select (id, a) on rth.{tb} to {user}@'%'",
    )
    url = f"mysql://{user}:rthcol@127.0.0.1:13480/rth"
    try:
        try:
            r = run_diff(get_dialect(url, side="A"), get_dialect(url, side="B"), ta, tb)
        except ValueError as exc:
            assert "secret" in str(exc) or "privilege" in str(exc).lower()
            return
        said = " ".join(r.warnings + r.schema_differences)
        assert not r.identical and ("secret" in said or "privilege" in said.lower())
    finally:
        mysql_run(root80, f"drop user if exists {user}@'%'")


def test_a_read_only_replica_is_diffed_normally(root80, tables):
    """`super_read_only` (a replica) changes nothing for a read-only tool."""
    ddl = "id int primary key, v varchar(10)"
    ta = tables(ROOT80, "ro_a", ddl, "insert into {t} values (1, 'a'), (2, 'b')")
    tb = tables(ROOT80, "ro_b", ddl, "insert into {t} values (1, 'a'), (2, 'c')")
    mysql_run(root80, "set global super_read_only = 1")
    try:
        r = run_diff(get_dialect(USER80, side="A"), get_dialect(USER80, side="B"), ta, tb)
    finally:
        mysql_run(root80, "set global super_read_only = 0", "set global read_only = 0")
    assert found(r) == [(2, "different", ("v",))]


# ====================================================================
# RT-HM4/5: ZEROFILL renders its display padding
# ====================================================================
#
# `cast(c as char)` of a ZEROFILL column keeps the display width: int(8)
# zerofill 5 renders `00000005`, decimal(8,3) zerofill 1.5 as a key renders
# `00001.5`. ZEROFILL is deprecated but common in legacy schemas, and the
# padding is display only - the value is 5, as the driver (and the sample
# check) says. Every row then differs from a plain integer on any engine.


@pytest.mark.xfail(strict=True, reason="RT-HM4: a ZEROFILL integer renders zero-padded")
def test_rt_hm4_zerofill_integer_equals_the_same_plain_integer(tables, mysql_url):
    ta = tables(mysql_url, "zf_a", "id int primary key, v int(8) zerofill",
                "insert into {t} values (1, 5), (2, 7)")  # fmt: skip
    tb = tables(mysql_url, "zf_b", "id int primary key, v int",
                "insert into {t} values (1, 5), (2, 8)")  # fmt: skip
    r = run_diff(
        get_dialect(mysql_url, side="A"), get_dialect(mysql_url, side="B"), ta, tb
    )
    assert found(r) == [(2, "different", ("v",))]


@pytest.mark.xfail(strict=True, reason="RT-HM4: a ZEROFILL integer renders zero-padded")
def test_rt_hm4_zerofill_integer_against_postgres(tables, pg_table, mysql_url, pg_url):
    r = _cross(
        tables, pg_table, mysql_url, pg_url,
        "id int primary key, v int(6) zerofill", ["insert into {t} values (1, 42), (2, 7)"],
        "id int primary key, v int", ["insert into {t} values (1, 42), (2, 8)"],
    )  # fmt: skip
    assert found(r) == [(2, "different", ("v",))]


@pytest.mark.xfail(strict=True, reason="RT-HM5: a ZEROFILL key part renders zero-padded")
@pytest.mark.parametrize(
    ("zf_ddl", "plain_ddl", "key", "rows_a", "rows_b", "expected"),
    [
        (
            "id int(6) zerofill not null, k2 int not null, v int, primary key (id, k2)",
            "id int not null, k2 int not null, v int, primary key (id, k2)",
            ["id", "k2"],
            "(1, 1, 10), (2, 1, 20)",
            "(1, 1, 10), (2, 1, 21)",
            [("2\x1f1", "different", ("v",))],
        ),
        (
            "id decimal(8,3) zerofill primary key, v int",
            "id decimal(8,3) primary key, v int",
            "id",
            "(1.5, 1), (2, 3)",
            "(1.5, 1), (2, 4)",
            [("2", "different", ("v",))],
        ),
    ],
    ids=["composite_int", "decimal"],
)
def test_rt_hm5_zerofill_key_matches_the_plain_key(
    tables, mysql_url, zf_ddl, plain_ddl, key, rows_a, rows_b, expected
):
    """Today every row is reported twice - only_in_a under `000002\\x1f1`
    (or `00002`), only_in_b under `2\\x1f1` - instead of the one change."""
    ta = tables(mysql_url, "zfk_a", zf_ddl, f"insert into {{t}} values {rows_a}")
    tb = tables(mysql_url, "zfk_b", plain_ddl, f"insert into {{t}} values {rows_b}")
    r = run_diff(
        get_dialect(mysql_url, side="A"),
        get_dialect(mysql_url, side="B"),
        ta,
        tb,
        key=key,
    )
    assert found(r) == expected


# ====================================================================
# RT-HM6: SET columns break the sample check across engines
# ====================================================================


@pytest.mark.sample_check_may_disagree
@pytest.mark.xfail(
    strict=True,
    reason="RT-HM6: the reference reads a MySQL SET as a Python set's repr",
)
def test_rt_hm6_set_column_against_postgres_text(tables, pg_table, mysql_url, pg_url):
    """mysql-connector returns a SET as a Python `set`, which the reference
    rules turn into `{'x', 'z'}` while the walk (correctly) renders `x,z`. So
    a SET against the same text on PostgreSQL makes the sample check report a
    failure on every non-empty row and the result is never identical - a
    false alarm on the walk's correct answer. The walk itself finds exactly
    the planted row."""
    r = _cross(
        tables, pg_table, mysql_url, pg_url,
        "id int primary key, s set('x','y','z')",
        ["insert into {t} values (1, 'z,x'), (2, 'y'), (3, '')"],
        "id int primary key, s text",
        ["insert into {t} values (1, 'x,z'), (2, 'z'), (3, '')"],
    )  # fmt: skip
    assert found(r) == [(2, "different", ("s",))]
    assert r.verification is not None and r.verification.failures == []


# ====================================================================
# RT-HM7: YEAR 0 renders as 0000
# ====================================================================


@pytest.mark.xfail(strict=True, reason="RT-HM7: YEAR 0 renders '0000', not the integer 0")
def test_rt_hm7_year_zero_against_an_integer_zero(tables, pg_table, mysql_url, pg_url):
    """YEAR is compared as an integer (2024 renders `2024`), but the year
    0000 renders `0000` while its value - and PostgreSQL's integer - is 0."""
    r = _cross(
        tables, pg_table, mysql_url, pg_url,
        "id int primary key, y year", ["insert into {t} values (1, 0), (2, 2024), (3, 1999)"],
        "id int primary key, y int", ["insert into {t} values (1, 0), (2, 2024), (3, 1998)"],
    )  # fmt: skip
    assert found(r) == [(3, "different", ("y",))]


# ====================================================================
# RT-HM8: JSON scalars of MySQL's own types render as strings
# ====================================================================


@pytest.mark.xfail(
    strict=True,
    reason="RT-HM8: a JSON DATE/TIME/DATETIME/BLOB scalar equals the JSON string of its text",
)
def test_rt_hm8_json_opaque_scalars_are_not_strings(tables, mysql_url):
    """MySQL JSON can hold DATE, TIME, DATETIME and binary scalars
    (`cast(date as json)`); JSON_TYPE tells them from strings and MySQL's own
    JSON comparison calls them unequal, but `cast(j as char)` prints each as
    a quoted string, so a document holding the date equals one holding the
    string. Debatable - no other engine has these types - but MySQL to MySQL
    it is a difference the tool cannot see, and the JSON warning does not
    mention it."""
    r = _mysql_pair(
        tables,
        mysql_url,
        "id int primary key, j json",
        [
            (
                "insert into {t} values (1, cast(cast('2024-01-02' as date) as json)), "
                "(2, cast(x'00ff' as json)), (3, cast(cast('10:00:00' as time) as json)), "
                "(4, json_array(cast('2024-01-02 03:04:05.5' as datetime(6))))"
            )
        ],
        [
            (
                "insert into {t} values (1, json_quote('2024-01-02')), "
                "(2, json_quote('base64:type15:AP8=')), (3, json_quote('10:00:00.000000')), "
                "(4, json_array('2024-01-02 03:04:05.500000'))"
            )
        ],
        tag="json_opaque",
    )
    assert [k for k, _, _ in found(r)] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    ("va", "vb"),
    [
        ("[1.0000000000000002]", "[1.0]"),
        ("[-0.0]", "[0.0]"),
        ("[1e2]", "[100]"),  # a double against an integer
        ('{"a": 1, "a": 2}', '{"a": 1}'),  # the last duplicate is kept
        ("[18446744073709551615]", "[18446744073709551614]"),
        ("[-9223372036854775808]", "[-9223372036854775807]"),
        ('["\\u001f"]', '["\\u001e"]'),
        ('["a\\\\b"]', '["a\\\\c"]'),
        ("[2.2250738585072014e-308]", "[2.225073858507201e-308]"),
    ],
)
def test_json_numbers_and_strings_that_differ_are_found(tables, mysql_url, va, vb):
    """MySQL prints JSON doubles as their shortest repr and integers exactly,
    so documents that differ in one number or one escape differ in text."""
    r = _mysql_pair(
        tables,
        mysql_url,
        "id int primary key, j json",
        [("insert into {t} values (1, %s), (2, '[0]')", (va,))],
        [("insert into {t} values (1, %s), (2, '[0]')", (vb,))],
        tag="json_num",
    )
    assert found(r) == [(1, "different", ("j",))]


# ====================================================================
# RT-HM9: the ENUM error value equals the empty member
# ====================================================================


@pytest.mark.xfail(
    strict=True,
    reason="RT-HM9: ENUM index 0 (an invalid value) and an '' member both render ''",
)
def test_rt_hm9_enum_error_value_is_not_the_empty_member(tables, mysql_url):
    """An invalid value written outside strict mode is stored as ENUM index
    0, MySQL's error value; an enum that also lists '' stores that member at
    its own index. Both print '' (`e + 0` is 0 and 2), so a row whose value
    was lost on insert equals one that really held ''."""
    r = _mysql_pair(
        tables,
        mysql_url,
        "id int primary key, e enum('a', '')",
        ["set session sql_mode = ''", "insert into {t} values (1, 'bogus'), (2, 'a')"],
        ["insert into {t} values (1, ''), (2, 'a')"],
        tag="enum0",
    )
    assert found(r) == [(1, "different", ("e",))]


# ====================================================================
# RT-HM10: the refusal messages depend on lc_messages being English
# ====================================================================


@pytest.mark.xfail(
    strict=True,
    reason="RT-HM10: a non-English lc_messages hides the explained refusal",
)
@pytest.mark.parametrize(
    ("ddl", "value", "expect"),
    [
        ("d decimal(65,0)", "1e60", r"does not fit DECIMAL\(65,6\)"),
        ("d double", "1e300", r"out of the comparable range"),
    ],
    ids=["decimal", "float"],
)
def test_rt_hm10_refusals_are_explained_under_any_lc_messages(
    tables, mysql_url, monkeypatch, ddl, value, expect
):
    """The DECIMAL, FLOAT and too-long guards raise through UUID_TO_BIN and
    `query` recognises them by the English text `... for function`. A server
    whose `lc_messages` is German (a GLOBAL default, or init_connect) words
    error 1411 differently, so the raw `Falscher string-Wert ... für
    Funktion uuid_to_bin` reaches the user without the explanation. The
    error number (1411) and the marker are language independent."""
    ta = tables(mysql_url, "lc_a", f"id int primary key, {ddl}",
                f"insert into {{t}} values (1, {value})")  # fmt: skip
    tb = tables(mysql_url, "lc_b", f"id int primary key, {ddl}",
                f"insert into {{t}} values (1, {value})")  # fmt: skip
    _init_hook(monkeypatch, "set session lc_messages = 'de_DE'")
    with pytest.raises(ValueError, match=expect):
        run_diff(
            get_dialect(mysql_url, side="A"), get_dialect(mysql_url, side="B"), ta, tb
        )


# ====================================================================
# RT-HM11: MariaDB is neither refused nor supported
# ====================================================================


@pytest.mark.xfail(
    strict=True,
    reason="RT-HM11: MariaDB connects, then fails with 'Illegal mix of collations' "
    "or 'FUNCTION uuid_to_bin does not exist'",
)
@pytest.mark.parametrize(
    "ddl",
    [
        "id int primary key, v varchar(10)",
        "id int primary key, v decimal(10,2)",
        "id int primary key, v double",
    ],
    ids=["text", "decimal", "float"],
)
def test_rt_hm11_mariadb_is_refused_by_name_or_compared(tables, ddl):
    """mysql:// accepts a MariaDB 11.8 server without a word. Its
    utf8mb4_uca1400_ai_ci default collation makes every text REPLACE an
    'Illegal mix of collations' and it has no UUID_TO_BIN, which every
    DECIMAL, FLOAT and binary guard calls - so the first checksum fails with
    an error naming neither MariaDB nor the problem. Integer, date and time
    columns compare correctly. Either refusing at connect, naming MariaDB,
    or supporting it is a fix."""
    rows = "(1, '1'), (2, '2')"
    planted = "(1, '1'), (2, '3')"
    ta = tables(MARIA, "maria_a", ddl, f"insert into {{t}} values {rows}")
    tb = tables(MARIA, "maria_b", ddl, f"insert into {{t}} values {planted}")
    try:
        r = run_diff(get_dialect(MARIA, side="A"), get_dialect(MARIA, side="B"), ta, tb)
    except ValueError as exc:
        assert "mariadb" in str(exc).lower()
        return
    assert found(r) == [(2, "different", ("v",))]


# ====================================================================
# RT-HM12: a --where subquery can lock the live table
# ====================================================================


@pytest.mark.xfail(
    strict=True,
    reason="RT-HM12: a FOR SHARE subquery in --where holds row locks for the whole diff",
)
@pytest.mark.parametrize("lock", ["for share", "lock in share mode"])
def test_rt_hm12_row_filter_cannot_take_shared_locks(tables, mysql_url, lock):
    """The session is READ ONLY, which refuses FOR UPDATE (error 1792) but
    not a shared locking read. `--where "id in (select id from t for share)"`
    passes the filter check, and InnoDB then holds S locks on every row it
    read until the diff's transaction ends - a writer on the live legacy
    table waits the whole run, which is exactly what the tool promises never
    to do (CLAUDE.md 4.9 refuses SQL Server's locking isolation levels for
    this reason). Refusing the locking clauses would fix it."""
    t = tables(mysql_url, "lock", "id int primary key, v int",
               "insert into {t} values (1, 1), (2, 2)")  # fmt: skip
    where = f"id in (select id from `{t}` {lock})"
    d = get_dialect(mysql_url, side="A")
    try:
        try:
            d.check_where(where)
        except ValueError:
            return  # refused before any query: fixed
        d.query(f"select count(*) {d.rows_from(t, where)}")
        writer = mysql_connect(mysql_url)
        try:
            mysql_run(
                writer,
                "set session innodb_lock_wait_timeout = 1",
                f"update `{t}` set v = v + 1 where id = 1",
            )
        finally:
            writer.close()
    finally:
        d.close()


WHERE_SPELLINGS = [
    ("region = 'eu'", [1, 3]),
    ('region = "eu"', [1, 3]),
    ("`region` = 'eu'", [1, 3]),
    ("region = _utf8mb4'eu'", [1, 3]),
    ("region = X'6575'", [1, 3]),
    ("region = n'eu'", [1, 3]),
    ("region = b'0110010101110101'", [1, 3]),
    ("region = 'e' 'u'", [1, 3]),
    ("region = {fn concat('e','u')}", [1, 3]),
    ("note = '#'", [3]),
    ("note = 'a\\\\b'", [1]),  # a doubled backslash, not before a quote
    ("region = 'e''u'", [4]),
    ('region = "e\'u"', [4]),
    ("region regexp '^e[u]$'", [1, 3]),
    ("id div 2 = 0", [1]),
    ("region = 'eu'\n", [1, 3]),
]


@pytest.mark.parametrize(
    ("where", "keys"), WHERE_SPELLINGS, ids=[w for w, _ in WHERE_SPELLINGS]
)
def test_mysql_spellings_in_a_row_filter(tables, mysql_url, where, keys):
    """MySQL's own literal forms - introducers, hex, bit, adjacent strings,
    ODBC escapes, `#` and `\\` inside strings - filter exactly the rows they
    select, on every query."""
    ddl = "id int primary key, region varchar(10), note varchar(10), v int"
    rows = [(1, "eu", "a\\b"), (2, "us", "x"), (3, "eu", "#"), (4, "e'u", "y")]
    ins = "insert into {t} values (%s, %s, %s, %s)"
    r = _mysql_pair(
        tables,
        mysql_url,
        ddl,
        [(ins, (i, g, n, i * 10)) for i, g, n in rows],
        [(ins, (i, g, n, i * 10 + 1)) for i, g, n in rows],
        tag="where",
        where_a=where,
        where_b=where,
    )
    assert found(r) == [(k, "different", ("v",)) for k in keys]


@pytest.mark.parametrize(
    "where",
    [
        "region = 'eu' # x",
        "region = 'eu' -- x",
        "region = 'eu' /*! or 1=1 */",
        "region = 'eu') or (1=1",
        "region = 'eu\\' or 1=1 or region = 'x'",
        "region = 'eu'; select 1",
        "region = '\\\\' or 1=1",
    ],
)
def test_mysql_row_filters_that_could_escape_are_refused(mysql_url, where):
    """A MySQL comment form, an executable comment, a closing parenthesis or
    a backslash-escaped quote is refused before any query, naming the side."""
    d = get_dialect(mysql_url, side="A")
    try:
        with pytest.raises(ValueError, match=r"side A.*row filter"):
            d.check_where(where)
    finally:
        d.close()


# ====================================================================
# RT-HM13: the sample check reads zero and invalid dates as NULL
# ====================================================================


@pytest.mark.xfail(
    strict=True,
    reason="RT-HM13: zero/invalid dates arrive as None, so a real difference is noted as a false alarm",
)
def test_rt_hm13_zero_date_difference_is_not_called_a_false_alarm(
    tables, pg_table, mysql_url, pg_url
):
    """The walk rightly reports MySQL's '0000-00-00' against a PostgreSQL
    NULL. mysql-connector returns None for any date Python cannot hold, the
    reference reads that as NULL, and the output tells the user the
    difference 'is not shown by the raw values (a false alarm ...)'. Such
    values should be outside the reference rules (UNCHECKED), not NULL."""
    r = _cross(
        tables, pg_table, mysql_url, pg_url,
        "id int primary key, d date",
        ["set session sql_mode = 'ALLOW_INVALID_DATES'",
         "insert into {t} values (1, '0000-00-00'), (2, '2024-02-31'), (3, '2024-01-01')"],
        "id int primary key, d date",
        ["insert into {t} values (1, null), (2, null), (3, '2024-01-02')"],
    )  # fmt: skip
    assert found(r) == [
        (1, "different", ("d",)),
        (2, "different", ("d",)),
        (3, "different", ("d",)),
    ]
    assert r.verification is not None
    assert not [n for n in r.verification.notes if "false alarm" in n]


# ====================================================================
# Regressions: sessions, values, schema
# ====================================================================

SESSION_STATEMENTS = [
    "set session default_collation_for_utf8mb4 = utf8mb4_general_ci",
    "set session max_sort_length = 4",
    "set session tmp_table_size = 1024",
    "set session big_tables = 1",
    "set session optimizer_switch = 'index_merge=off,mrr=off,block_nested_loop=off,hash_join=off'",
    "set session character_set_results = NULL",
    "set session autocommit = 0",
    "set session transaction_isolation = 'READ-UNCOMMITTED'",
    "set session div_precision_increment = 30",
    "set session group_concat_max_len = 4",
    "set session sql_auto_is_null = 1",
    "set session cte_max_recursion_depth = 0",
    "set session time_zone = 'SYSTEM'",
    "set session max_execution_time = 60000",
    "set session lc_time_names = 'de_DE'",
    "set session sql_mode = 'ANSI,NO_BACKSLASH_ESCAPES,PIPES_AS_CONCAT,REAL_AS_FLOAT'",
    "set session collation_connection = utf8mb4_bin",
    "set session windowing_use_high_precision = 0",
]


@pytest.mark.parametrize("statement", SESSION_STATEMENTS)
def test_hostile_session_defaults_do_not_move_the_verdict(
    tables, mysql_url, monkeypatch, statement
):
    """Settings a DBA can make a login's default (init_connect, GLOBAL), each
    on a table of long text keys, JSON, DECIMAL, binary and timestamps with
    one planted change."""
    ddl = (
        "id varchar(300) primary key, v varchar(20), d decimal(65,0), "
        "j json, b varbinary(10), ts timestamp(6) null, f double"
    )
    rows = [
        ("x" * 200 + "A", "same", 1, '{"a": 1}', b"\x00", "2024-06-15 12:00:00.5", 0.1),
        ("x" * 200 + "B", "old", 2, "[1]", b"\x01", None, 1e15 + 0.5),
    ]
    ins = "insert into {t} values (%s, %s, %s, %s, %s, %s, %s)"
    ta = tables(mysql_url, "sess_a", ddl, "set time_zone = '+00:00'",
                *[(ins, r) for r in rows])  # fmt: skip
    rows[1] = (rows[1][0], "new", *rows[1][2:])
    tb = tables(mysql_url, "sess_b", ddl, "set time_zone = '+00:00'",
                *[(ins, r) for r in rows])  # fmt: skip
    _init_hook(monkeypatch, statement)
    r = run_diff(
        get_dialect(mysql_url, side="A"), get_dialect(mysql_url, side="B"), ta, tb
    )
    assert found(r) == [("x" * 200 + "B", "different", ("v",))]


@pytest.mark.parametrize(
    ("n", "engine", "typ"),
    [(1016, "innodb", "int"), (4000, "myisam", "tinyint")],
    ids=["innodb_max", "myisam_4000"],
)
def test_widest_tables_find_the_last_column(tables, mysql_url, n, engine, typ):
    """InnoDB's 1,017-column maximum and MyISAM's 4,096 (minus the key): the
    nested concat and per-column guards still fit one query."""
    cols = ", ".join(f"c{i} {typ}" for i in range(n))
    same = ", ".join("1" for _ in range(n))
    last = ", ".join("1" if i < n - 1 else "2" for i in range(n))
    ddl = f"id int primary key, {cols}) engine={engine} -- "
    r = _mysql_pair(
        tables,
        mysql_url,
        ddl,
        [f"insert into {{t}} values (1, {same}), (2, {same})"],
        [f"insert into {{t}} values (1, {same}), (2, {last})"],
        tag=f"wide_{engine}",
    )
    assert found(r) == [(2, "different", (f"c{n - 1}",))]


@pytest.mark.parametrize(
    ("ddl", "va", "vb"),
    [
        ("float", -1e-9, 1e-9),  # both 0.000000: equal, so plant elsewhere
        ("double", -5e-7, -4.9999999e-7),  # -0.000001 against 0.000000
        ("double", 9007199254740993.0, 9007199254740994.0),  # the same double
        ("double", 2.0**53 + 2, 2.0**53 + 4),
        ("double", -(2.0**60), -(2.0**60) - 256),
        ("float", 16777217.0, 16777218.0),  # float4: 16777216 against 16777218
        ("decimal(20,10)", "-0.0000005", "-0.0000004"),
        (
            "decimal(65,30)",
            "0.000000500000000000000000000000",
            "0.000000499999999999999999999999",
        ),
        ("decimal(65,0)", "9" * 59, "9" * 58 + "8"),
        ("bit(64)", 2**64 - 1, 2**64 - 2),
        ("bigint unsigned", 2**64 - 1, 2**64 - 2),
        ("time(6)", "-838:59:59", "838:59:59"),
        ("time(6)", "-00:00:00.000001", "00:00:00.000001"),
        ("datetime(6)", "1000-01-01 00:00:00.000001", "1000-01-01 00:00:00.000002"),
        ("year", 1901, 2155),
        ("binary(4)", b"ab", b"ab\x00\x01"),
        ("char(5)", "ab", "ab\t"),
        ("varchar(5) collate utf8mb4_0900_ai_ci", "a", "A"),
        ("varchar(5) collate utf8mb4_general_ci", "a ", "a"),
        ("enum('sad','ok','Ok') collate utf8mb4_bin", "ok", "Ok"),
        ("set('a','b','c')", "a,c", "a,b,c"),
        ("point", "point(1 2)", "point(1 2.0000000001)"),
    ],
)
def test_values_that_must_differ_do(tables, mysql_url, ddl, va, vb):
    """Pairs at the edges of each MySQL type: the planted pair is reported
    exactly when the rules say the values differ (the first and third pairs
    are equal by the rules - the same text at six places, the same double),
    and in every case row 2 carries a planted change too."""
    spatial = ddl == "point"
    put = "st_geomfromtext(%s)" if spatial else "%s"
    ins = f"insert into {{t}} values (%s, {put}, %s)"
    if ddl.startswith("bit"):
        va, vb = (v.to_bytes(8, "big") for v in (va, vb))
    r = _mysql_pair(
        tables,
        mysql_url,
        f"id int primary key, v {ddl}, w int",
        [(ins, (1, va, 0)), (ins, (2, va, 0))],
        [(ins, (1, vb, 0)), (ins, (2, va, 1))],
        tag="edge",
    )
    if ddl in ("float", "double") and _float_text(ddl, va) == _float_text(ddl, vb):
        assert found(r) == [(2, "different", ("w",))]
    else:
        assert found(r) == [(1, "different", ("v",)), (2, "different", ("w",))]


def _float_text(ddl: str, x: float) -> str:
    """The canonical text of `x` stored in a MySQL `ddl` column (float4 rounds)."""
    import struct

    from parity.dialects.base import canonical_float_text

    if ddl == "float":
        x = struct.unpack("<f", struct.pack("<f", x))[0]
    return canonical_float_text(x, 6)


def test_mediumint_unsigned_key_against_postgres_bigint(
    tables, pg_table, mysql_url, pg_url
):
    """The narrow unsigned key types bucket as integers against a wider one."""
    r = _cross(
        tables, pg_table, mysql_url, pg_url,
        "id mediumint unsigned primary key, v int",
        ["insert into {t} values (16777215, 1), (0, 2), (8388608, 3)"],
        "id bigint primary key, v int",
        ["insert into {t} values (16777215, 1), (0, 3), (8388608, 3)"],
    )  # fmt: skip
    assert found(r) == [(0, "different", ("v",))]


def test_storage_engines_without_transactions_are_compared_with_a_caveat(
    tables, mysql_url
):
    """ARCHIVE and CSV, besides MyISAM and MEMORY: the walk is right on quiet
    data and each side says it is not one snapshot."""
    a = tables(mysql_url, "arch_a", "id int not null, v varchar(10)) engine=archive -- ",
               "insert into {t} values (1, 'a'), (2, 'b')")  # fmt: skip
    b = tables(mysql_url, "csv_b", "id int not null, v varchar(10) not null) engine=csv -- ",
               "insert into {t} values (1, 'a'), (2, 'c')")  # fmt: skip
    r = run_diff(get_dialect(mysql_url, side="A"), get_dialect(mysql_url, side="B"), a, b)
    assert found(r) == [(2, "different", ("v",))]
    caveats = " ".join(r.warnings)
    assert "ARCHIVE" in caveats and "CSV" in caveats


# ====================================================================
# Versions: RT-HM1/HM2/HM4 on MySQL 8.0, 8.4 and 9.x (Docker)
# ====================================================================

#: Plain logins on `rth-mysql-80`, `rth-mysql-84` and `rth-mysql-97` (9.7,
#: component_classic_hashing installed by root so MD5() exists).
VERSION_URLS = [
    u.strip()
    for u in os.environ.get(
        "PARITY_RTH_MYSQL_VERSIONS",
        "mysql://rth:rth@127.0.0.1:13480/rth,mysql://rth:rth@127.0.0.1:13484/rth,"
        "mysql://rth:rth@127.0.0.1:13497/rth",
    ).split(",")
]

VERSION_CASES = [
    ("sjis", "v varchar(10) character set sjis", "_sjis x'F040'", "_sjis x'F041'"),
    (
        "ucs2",
        "v varchar(10) character set ucs2",
        "_ucs2 x'D83DDE00'",
        "_ucs2 x'D83DDE01'",
    ),
]


@pytest.mark.xfail(strict=True, reason="RT-HM1/RT-HM2: lossy conversion on every version")
@pytest.mark.parametrize(
    "url", VERSION_URLS, ids=lambda u: u.rsplit(":", 1)[1].split("/")[0]
)
@pytest.mark.parametrize(
    ("label", "col", "va", "vb"), VERSION_CASES, ids=[c[0] for c in VERSION_CASES]
)
def test_rt_hm1_hm2_on_every_version(tables, url, label, col, va, vb):
    r = _mysql_pair(
        tables, url, f"id int primary key, {col}",
        [f"insert into {{t}} values (1, {va}), (2, 'same')"],
        [f"insert into {{t}} values (1, {vb}), (2, 'same')"],
        tag=f"ver_{label}",
    )  # fmt: skip
    assert found(r) == [(1, "different", ("v",))]


@pytest.mark.parametrize(
    "url", VERSION_URLS, ids=lambda u: u.rsplit(":", 1)[1].split("/")[0]
)
def test_identifiers_are_case_sensitive_on_a_case_sensitive_server(tables, url):
    """lower_case_table_names=0 (Linux): `t_Case` and `t_case` are two tables,
    and each side's columns come from exactly the one it names."""
    conn = mysql_connect(url)
    try:
        if mysql_run(conn, "select @@lower_case_table_names")[0][0][0] != 0:
            pytest.skip("this server folds table names")
    finally:
        conn.close()
    up = tables(url, "Case", "id int primary key, upper_only int, v int",
                "insert into {t} values (1, 0, 10), (2, 0, 20)")  # fmt: skip
    low = tables(url, "case", "id int primary key, v int, lower_only int",
                 "insert into {t} values (1, 10, 0), (2, 21, 0)")  # fmt: skip
    r = run_diff(get_dialect(url, side="A"), get_dialect(url, side="B"), up, low)
    assert found(r) == [(2, "different", ("v",))]
    assert r.schema_differences == [
        "not compared, present only on side A: ['upper_only']",
        "not compared, present only on side B: ['lower_only']",
    ]


@pytest.mark.xfail(
    strict=True,
    reason="RT-HM14: a MySQL 9 VECTOR column kills the run with 'Incorrect arguments to cast_as_char'",
)
def test_rt_hm14_vector_column_is_compared_or_refused_by_name(tables):
    """MySQL 9 added VECTOR. It is unmapped, so it falls through to
    `cast(c as char)`, which MySQL refuses for a vector (error 1210) - the
    first checksum dies naming neither the column nor the type. Rendering it
    like binary (its stored floats as hex) or refusing it by name up front,
    with --exclude as the way out, would both do."""
    url = VERSION_URLS[-1]
    conn = mysql_connect(url)
    try:
        if not mysql_run(conn, "select version()")[0][0][0].startswith("9."):
            pytest.skip("VECTOR needs MySQL 9")
    finally:
        conn.close()
    ddl = "id int primary key, v vector(2)"
    ta = tables(url, "vec_a", ddl, "insert into {t} values (1, string_to_vector('[1,2]')), "
                "(2, string_to_vector('[1,2]'))")  # fmt: skip
    tb = tables(url, "vec_b", ddl, "insert into {t} values (1, string_to_vector('[1,2]')), "
                "(2, string_to_vector('[1,3]'))")  # fmt: skip
    try:
        r = run_diff(get_dialect(url, side="A"), get_dialect(url, side="B"), ta, tb)
    except ValueError as exc:
        assert "'v'" in str(exc) and "vector" in str(exc).lower()
        return
    assert found(r) == [(2, "different", ("v",))]


@pytest.mark.xfail(
    strict=True,
    reason="RT-HM15: a table the login may not read is reported as 'table not found'",
)
def test_rt_hm15_unreadable_table_is_not_called_missing(root80, tables):
    """MySQL hides a table the login holds no privilege on from
    information_schema, so the tool says `table not found: t (looked in
    schema 'rth')` - sending the user to check the name instead of the
    grants. A `select 1 from t limit 0` probe answers 1142 'SELECT command
    denied', which is what `_exists_but_unreadable` exists to report."""
    t = tables(
        ROOT80, "nogrant", "id int primary key, v int", "insert into {t} values (1, 1)"
    )
    other = tables(ROOT80, "granted", "id int primary key")
    user = f"rth_ng_{RUN}"
    mysql_run(
        root80,
        f"drop user if exists {user}@'%'",
        f"create user {user}@'%' identified by 'rthng'",
        f"grant select on rth.{other} to {user}@'%'",
    )
    url = f"mysql://{user}:rthng@127.0.0.1:13480/rth"
    try:
        with pytest.raises(ValueError, match=r"side A.*(denied|privilege|permission)"):
            run_diff(get_dialect(url, side="A"), get_dialect(USER80, side="B"), t, t)
    finally:
        mysql_run(root80, f"drop user if exists {user}@'%'")


@pytest.mark.xfail(
    strict=True,
    reason="RT-HM16: Ctrl-C cannot stop a MySQL query when the login is at max_user_connections",
)
def test_rt_hm16_cancel_works_at_the_connection_limit(root80):
    """`cancel` (Ctrl-C) opens a second connection as the same login to KILL
    QUERY. A login whose max_user_connections is already used up - common
    for service accounts, and reached whenever both sides use one login with
    a limit of 2 - cannot open it, the failure is swallowed, and the running
    checksum continues to its end: a Ctrl-C that does nothing for as long as
    a full-table hash pass takes. Interrupting through the connection's own
    socket, or keeping the KILL connection open from the start, would not
    need a free slot."""
    import threading
    import time

    user = f"rth_mc_{RUN}"
    mysql_run(
        root80,
        f"drop user if exists {user}@'%'",
        f"create user {user}@'%' identified by 'rthmc' with max_user_connections 1",
        f"grant select on rth.* to {user}@'%'",
    )
    try:
        d = get_dialect(f"mysql://{user}:rthmc@127.0.0.1:13480/rth", side="A")
        try:
            timer = threading.Timer(0.5, d.cancel)
            timer.start()
            t0 = time.monotonic()
            d.query("select sleep(8)")
            elapsed = time.monotonic() - t0
            timer.join()
        finally:
            d.close()
    finally:
        mysql_run(root80, f"drop user if exists {user}@'%'")
    assert elapsed < 4


def test_cancel_stops_a_running_query(root80):
    """The control for RT-HM16: with a free connection slot, KILL QUERY lands
    within a second (a killed SLEEP returns 1 rather than raising)."""
    import threading
    import time

    d = get_dialect(USER80, side="A")
    try:
        timer = threading.Timer(0.5, d.cancel)
        timer.start()
        t0 = time.monotonic()
        d.query("select sleep(8)")
        elapsed = time.monotonic() - t0
        timer.join()
    finally:
        d.close()
    assert elapsed < 4


ESCAPE_COLLATIONS = [
    "utf8mb4_0900_ai_ci",
    "utf8mb4_0900_as_cs",
    "utf8mb4_ja_0900_as_cs_ks",
    "utf8mb4_general_ci",
    "utf8mb4_unicode_520_ci",
    "utf8mb4_de_pb_0900_ai_ci",
    "utf8mb4_tr_0900_ai_ci",
    "utf8mb4_bin",
    "character set latin1 collate latin1_german2_ci",
    "character set utf8mb3 collate utf8mb3_general_ci",
    "character set ucs2 collate ucs2_general_ci",
    "character set utf16 collate utf16_unicode_ci",
    "character set utf32 collate utf32_bin",
]


@pytest.mark.parametrize("coll", ESCAPE_COLLATIONS)
def test_escaping_holds_under_every_collation(tables, mysql_url, coll):
    """REPLACE is byte-exact under every collation tried, ignorable-character
    and accent-blind ones included, so the separator and the sentinel stay
    unforgeable: `x<US>y` against `x\\sy`, `\\N` against NULL, and a
    backslash pair, each planted on its own row."""
    spec = coll if coll.startswith("character") else f"collate {coll}"
    ddl = f"id int primary key, v varchar(20) {spec}"
    ins = "insert into {t} values (%s, %s)"
    a = [(1, "x\x1fy"), (2, "\\N"), (3, "a\\\\b"), (4, "same")]
    b = [(1, "x\\sy"), (2, None), (3, "a\\b"), (4, "same")]
    r = _mysql_pair(
        tables, mysql_url, ddl, [(ins, row) for row in a], [(ins, row) for row in b],
        tag="esc",
    )  # fmt: skip
    assert found(r) == [(k, "different", ("v",)) for k in (1, 2, 3)]


@pytest.mark.xfail(
    strict=True,
    reason="RT-HM17: a view over a MyISAM table carries no no-snapshot caveat",
)
@pytest.mark.parametrize("engine", ["myisam", "memory"])
def test_rt_hm17_view_over_a_non_transactional_table_says_so(tables, mysql_url, engine):
    """The no-snapshot caveat (RT-D3) is read from information_schema.tables
    for the named table only, and a view reports engine NULL - so diffing a
    view over a MyISAM or MEMORY table prints nothing, though every level
    reads the base table afresh and a write mid-walk can make the result
    wrong. The view's base tables (information_schema.view_table_usage, 8.0.13+)
    should carry the caveat through."""
    base = tables(mysql_url, f"vb_{engine}", f"id int primary key, v int) engine={engine} -- ",
                  "insert into {t} values (1, 1), (2, 2)")  # fmt: skip
    other = tables(mysql_url, "vo", "id int primary key, v int",
                   "insert into {t} values (1, 1), (2, 3)")  # fmt: skip
    view = f"rth_mysql_view_{engine}_{RUN}"
    conn = mysql_connect(mysql_url)
    try:
        mysql_run(conn, f"create or replace view `{view}` as select * from `{base}`")
        r = run_diff(
            get_dialect(mysql_url, side="A"),
            get_dialect(mysql_url, side="B"),
            view,
            other,
        )
    finally:
        mysql_run(conn, f"drop view if exists `{view}`")
        conn.close()
    assert found(r) == [(2, "different", ("v",))]
    assert any(engine.upper() in w.upper() and "snapshot" in w for w in r.warnings)


CROSS_KEYS = [
    (
        "bigint unsigned", "numeric(20)",
        ["18446744073709551615", "0", "9223372036854775808"], "0",
    ),
    ("decimal(20,0)", "bigint", ["-9223372036854775808", "0", "42"], "0"),
    ("decimal(10,2)", "numeric", ["-0.00", "1.50", "2.25"], "1.5"),
    (
        "datetime(6)", "timestamp",
        ["'2024-01-01 00:00:00.000001'", "'2024-01-01'", "'1000-01-01 23:59:59.999999'"],
        "2024-01-01 00:00:00.000000",
    ),
    ("time(6)", "time", ["'10:00:00.5'", "'23:59:59'", "'00:00:00'"], "23:59:59.000000"),
    (
        "binary(4)", "bytea",
        ["x'00112233'", "x'ff112233'", "x'00000000'"], "\\\\xff112233",
    ),
    (
        "char(36)", "uuid",
        ["'0e2d9a6e-9c1b-4d7c-8f0a-1b2c3d4e5f60'", "'1e2d9a6e-9c1b-4d7c-8f0a-1b2c3d4e5f60'",
         "'2e2d9a6e-9c1b-4d7c-8f0a-1b2c3d4e5f60'"],
        "1e2d9a6e-9c1b-4d7c-8f0a-1b2c3d4e5f60",
    ),
    ("varchar(10) collate utf8mb4_bin", "text", ["'a'", "'A'", "'é'"], "A"),
]  # fmt: skip


@pytest.mark.parametrize(
    ("my_type", "pg_type", "keys", "planted"), CROSS_KEYS, ids=[c[0] for c in CROSS_KEYS]
)
def test_key_types_match_across_mysql_and_postgres(
    tables, pg_table, mysql_url, pg_url, my_type, pg_type, keys, planted
):
    """Every MySQL key family against its PostgreSQL counterpart: rows pair
    up by exact key text (a -0.00 decimal is 0, an integer key equals an
    integral numeric one), and only the planted row differs."""
    pg_keys = ["'\\x" + k[2:-1] + "'::bytea" if pg_type == "bytea" else k for k in keys]
    planted_at = 1
    my_rows = [f"insert into {{t}} values ({k}, {i})" for i, k in enumerate(keys)]
    pg_rows = [
        f"insert into {{t}} values ({k}, {i + (1 if i == planted_at else 0)})"
        for i, k in enumerate(pg_keys)
    ]
    r = _cross(
        tables, pg_table, mysql_url, pg_url,
        f"id {my_type} primary key, v int", my_rows,
        f"id {pg_type} primary key, v int", pg_rows,
    )  # fmt: skip
    assert found(r) == [(planted, "different", ("v",))]


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"bisection_factor": 2, "threshold": 1},
        {"bisection_factor": 1024, "threshold": 50_000},
    ],
    ids=["default", "binary_split", "wide_split"],
)
def test_scale_first_last_middle_and_far_keys(tables, mysql_url, kw):
    """100,000 rows a side (run at 1,000,000 by hand: same five differences
    at every setting, 38 queries by default): a change in the first key, the
    last key and one microsecond in the middle, a deleted row and one
    inserted at 2^62, which widens the key range a millionfold."""
    n = 100_000
    gen = (
        "insert into {t} with recursive n(i) as (select 1 union all select i + 1 "
        f"from n where i < {n}) select i, concat('row', i), i / 7, "
        "timestampadd(second, i, '2020-01-01'), md5(i) from n"
    )
    ddl = "id bigint primary key, s varchar(40), d decimal(18,4), ts datetime(6), u char(32)"
    pre = "set session cte_max_recursion_depth = 1000000"
    ta = tables(mysql_url, "scale_a", ddl, pre, gen)
    tb = tables(
        mysql_url, "scale_b", ddl, pre, gen,
        "update {t} set s = 'first' where id = 1",
        f"update {{t}} set d = d + 0.0001 where id = {n}",
        f"delete from {{t}} where id = {n // 2}",
        "insert into {t} values (4611686018427387904, 'far', 0, '2020-01-01', 'x')",
        "update {t} set ts = timestampadd(microsecond, 1, ts) where id = 77777",
    )  # fmt: skip
    r = run_diff(
        get_dialect(mysql_url, side="A"), get_dialect(mysql_url, side="B"), ta, tb, **kw
    )
    assert found(r) == [
        (1, "different", ("s",)),
        (n // 2, "only_in_a", ("d", "s", "ts", "u")),
        (77777, "different", ("ts",)),
        (n, "different", ("d",)),
        (4611686018427387904, "only_in_b", ("d", "s", "ts", "u")),
    ]


@pytest.mark.xfail(
    strict=True,
    reason="RT-HM18: casefold matches differently spelled columns (straße = strasse)",
)
def test_rt_hm18_columns_that_differ_beyond_case_are_not_matched(
    tables, pg_table, mysql_url, pg_url
):
    """Column matching is meant to bridge *case* (CLAUDE.md 4.4), but
    `str.casefold` also folds ß to ss, the ﬁ ligature to fi and the Kelvin
    sign to k. A MySQL column `straße` (MySQL itself keeps `straße` and
    `strasse` apart in one table) is then silently compared with a
    PostgreSQL column `strasse`, and reported under A's name, with no schema
    difference. `str.lower` would bridge case only."""
    r = _cross(
        tables, pg_table, mysql_url, pg_url,
        "id int primary key, `straße` int", ["insert into {t} values (1, 1), (2, 2)"],
        'id int primary key, "strasse" int', ["insert into {t} values (1, 1), (2, 3)"],
    )  # fmt: skip
    assert r.schema_differences
