"""Red-team round H, Snowflake (findings RT-HF<n>).

Every test plants a difference and asserts that exactly it is found, or
asserts a refusal. A confirmed bug is a strict xfail until it is fixed, so it
turns green the moment it is.

The live tests run against the account in PARITY_TEST_SNOWFLAKE (skipped
otherwise, as in tests/test_snowflake.py). They work in their own schema,
RTH_SF_HT, created for the module and dropped with everything in it at the
end, and keep every table to a few hundred rows: the account is a trial with
limited credits. Nothing here changes an account, user, role or warehouse
setting. A "user default" (ALTER USER ... SET <parameter>) is simulated by
adding the parameter to the login's `session_parameters` - exactly where a
user or account default is in force: before the dialect's own pins run.

The offline tests at the top need no account and run in CI.
"""

from __future__ import annotations

import contextlib
import io
import json
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from conftest import open_snowflake

import parity.dialects.snowflake_dialect as sd
from parity.cli import main
from parity.dialects.snowflake_dialect import SnowflakeDialect, map_type_snowflake
from parity.engine import diff
from parity.types import Column, LogicalType

SCHEMA = "RTH_SF_HT"


# --------------------------------------------------------------------- offline


@pytest.mark.parametrize(
    "predicate",
    [
        # Accidental: everything after `//` - the closing parenthesis and the
        # tool's own bucket conditions - is commented out, and the run dies
        # with a bare "syntax error ... unexpected '<EOF>'" (verified live).
        "id < 60 // only the early rows",
        # Crafted: the guard counts the parentheses inside the comments,
        # Snowflake does not, so the predicate closes the wrapper the tool put
        # around it and the bucket bounds are no longer ANDed in. Verified
        # live: the per-level row-count check then reads the ranges as
        # unexplained (not identical), so it is caught downstream - but the
        # guard exists so that a predicate is read the way it was checked.
        "id < 40 // (\n) or (1=0 // )\n or 1=0",
    ],
)
def test_rt_hf10_a_double_slash_comment_is_refused(predicate):
    """RT-HF10: a `//` comment in a row filter is refused."""
    with pytest.raises(ValueError, match="//"):
        SnowflakeDialect(side="A").check_where(predicate)


def test_rt_hf3_offline_a_nanosecond_tz_key_is_moved_to_utc():
    """The FF9 branch of `normalize_key` formats the raw column, so a
    TIMESTAMP_TZ key prints in its stored offset. The FF6 branch (through
    `normalize`) converts to UTC first; the FF9 one must too."""
    col = Column("K", map_type_snowflake("TIMESTAMP_TZ(9)", None), "TIMESTAMP_TZ(9)")
    sql = SnowflakeDialect(side="A").normalize_key(col)
    nine = sql[sql.index("else ") :]
    assert "convert_timezone" in nine, nine


def _int_key() -> Any:
    """A one-column NUMBER(38,0) key spec on column K."""
    from parity.types import KeySpec

    return KeySpec([Column("K", map_type_snowflake("NUMBER", 0), "NUMBER")], False)


def test_rt_hf9_offline_a_span_that_fits_keeps_the_shared_sql():
    """Every span whose `(k - lo) * n` fits 38 digits - all of BIGINT's, and
    NUMBER(38,0) keys up to ~3e36 at 32 segments - emits the shared bucket
    SQL byte for byte, so no checksum moved."""
    from parity.dialects.base import Dialect

    d = SnowflakeDialect(side="A")
    key = _int_key()
    for lo, hi, n in [
        (0, 10, 4),
        (-(2**63), 2**63, 32),
        (0, 10**36, 32),
        (5, 3 * 10**36, 33),
    ]:
        assert d.bucket_expr(key, '"K"', lo, hi, n) == Dialect.bucket_expr(
            d, key, '"K"', lo, hi, n
        )


@pytest.mark.parametrize(
    ("lo", "hi", "n"),
    [
        (0, 10**37 + 1, 32),
        (-(10**38) + 1, 10**38, 32),
        (-(10**38) + 1, 10**38, 7),
        (3, 10**38 - 1, 2),
    ],
)
def test_rt_hf9_offline_a_wide_span_buckets_by_its_boundaries(lo, hi, n):
    """The boundary form puts every key in the bucket `engine.bucket_bounds`
    says it is in - checked at each boundary, one either side of it, and at
    random keys - and writes no literal outside [lo, hi)."""
    import random
    import re

    from parity.engine import bucket_bounds

    sql = SnowflakeDialect(side="A").bucket_expr(_int_key(), '"K"', lo, hi, n)
    arms = [(int(v), int(q)) for v, q in re.findall(r">= (-?\d+) then (\d+)", sql)]
    assert len(arms) == n - 1 and all(lo <= v < hi for v, _ in arms)
    assert "*" not in sql and "/" not in sql

    def bucket(k: int) -> int:
        """The bucket the boundary arms put key `k` in."""
        return next((q for v, q in arms if k >= v), 0)

    rng = random.Random(lo ^ hi ^ n)
    keys = {lo, hi - 1, *(rng.randrange(lo, hi) for _ in range(500))}
    for i in range(n):
        b_lo, b_hi = bucket_bounds(i, lo, hi, n)
        keys |= {b_lo - 1, b_lo, b_lo + 1, b_hi - 1}
    for k in keys:
        if lo <= k < hi:
            q = bucket(k)
            b_lo, b_hi = bucket_bounds(q, lo, hi, n)
            assert b_lo <= k < b_hi, (k, q)


def test_rt_hf8_offline_the_decimal_text_never_casts_to_number_38():
    """RT-HF8: the DECIMAL rendering rounds in the column's own type."""
    col = Column("W", LogicalType.DECIMAL, "NUMBER")
    for scale in (0, 6, 20):
        sql = SnowflakeDialect(side="A", float_scale=scale).normalize(col)
        assert "number(38" not in sql and f'round("W", {scale})' in sql


def test_rt_hf7_offline_spatial_and_vector_render_without_a_varchar_cast():
    """RT-HF7: spatial and VECTOR columns render through their own functions."""
    for raw, needle in [
        ("GEOGRAPHY", 'st_aswkb("V")'),
        ("GEOMETRY", 'st_asewkb("V")'),
        ("VECTOR(FLOAT, 3)", 'to_json(cast("V" as array))'),
    ]:
        col = Column("V", map_type_snowflake(raw, None), raw)
        sql = SnowflakeDialect(side="A").normalize(col)
        assert needle in sql and "as varchar)" not in sql.replace(needle, ""), raw


def _connect_log(monkeypatch) -> list[str]:
    """Connect a SnowflakeDialect to a fake connector; return its statements."""
    import sys
    import types

    log: list[str] = []

    class Cur:
        def execute(self, sql: str) -> None:
            """Record the statement."""
            log.append(sql)

        def close(self) -> None:
            """Nothing to release."""

    class Conn:
        def cursor(self) -> Cur:
            """A fresh recording cursor."""
            return Cur()

    connector = types.ModuleType("snowflake.connector")
    connector.connect = lambda **kw: Conn()  # type: ignore[attr-defined]
    package = types.ModuleType("snowflake")
    package.connector = connector  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "snowflake", package)
    monkeypatch.setitem(sys.modules, "snowflake.connector", connector)
    SnowflakeDialect(side="A").connect("snowflake://u:p@acct/db/sch?warehouse=w")
    return [s.lower().replace(" ", "") for s in log]


def test_rt_hf2_offline_quoted_identifiers_ignore_case_is_pinned(monkeypatch):
    """RT-HF2: QUOTED_IDENTIFIERS_IGNORE_CASE is pinned off at connect."""
    log = _connect_log(monkeypatch)
    assert any("quoted_identifiers_ignore_case=false" in s for s in log), log


def test_rt_hf6_offline_strict_json_output_is_pinned(monkeypatch):
    """RT-HF6: STRICT_JSON_OUTPUT is pinned off at connect."""
    log = _connect_log(monkeypatch)
    assert any("strict_json_output=false" in s for s in log), log


def test_rt_hf11_offline_a_secret_url_never_shows_its_password():
    """The conftest URL type masks the password wherever pytest would print
    it - repr() of a fixture argument and saferepr() in an assertion's
    explanation - while handing the driver the real value."""
    from _pytest._io.saferepr import saferepr
    from conftest import SecretUrl, redact_url

    secret = "Rth-" + uuid.uuid4().hex
    url = SecretUrl(f"snowflake://me:{secret}@acct-x/db/sch?warehouse=w")
    shown = [repr(url), saferepr(url), repr([url]), repr({"u": url}), redact_url(url)]
    leaked = [secret in s for s in shown]
    assert leaked == [False] * len(shown)
    assert str(url).endswith("?warehouse=w") and secret in f"{url}"
    assert urlsplit(url).password == secret


def test_the_existing_pins_are_still_made(monkeypatch):
    """Control for the two tests above: the fake really records the pins."""
    log = _connect_log(monkeypatch)
    assert "altersessionsettimezone='utc'" in log
    assert "altersessionsetrows_per_resultset=0" in log


# ------------------------------------------------------------------------ live


class _Url(str):
    """A URL that never shows itself. pytest prints a failing test's fixture
    arguments with repr(), and a plain string here would put the password and
    the account in the failure report (it did, once, in this campaign)."""

    def __repr__(self) -> str:
        """Never show the URL: it holds the password and the account."""
        return "<snowflake url>"


def _with_schema(url: str, schema: str) -> str:
    """The same URL with its schema replaced by `schema`, still redacted."""
    p = urlsplit(url)
    database = next(x for x in p.path.split("/") if x)
    return _Url(
        urlunsplit((p.scheme, p.netloc, f"/{database}/{schema}", p.query, p.fragment))
    )


def test_rt_hf11_the_url_fixture_never_shows_its_password(snowflake_url):
    """Leaked secret (test suite). pytest prints a failing test's arguments
    with repr(); `snowflake_url` (and the BigQuery, SQL Server, MySQL ones)
    is the raw URL, so one failure in tests/test_snowflake.py puts
    `user:password@account` into the console and any CI log. Seen in this
    campaign. A str subclass with a masking __repr__ (`_Url` here) fixes it."""
    password = urlsplit(snowflake_url).password or ""
    shown = repr(snowflake_url)
    leaked = bool(password) and password in shown
    assert not leaked, "repr() of the fixture shows the password"


@pytest.fixture(scope="module")
def sf_url(snowflake_url) -> str:
    """The configured URL, hidden from pytest's failure reports."""
    return _Url(snowflake_url)


@pytest.fixture(scope="module")
def sfh(snowflake_url) -> Iterator[str]:
    """A URL whose schema is this module's own, created here and dropped at the
    end with everything in it (tables, views, policies, functions)."""
    admin = open_snowflake(snowflake_url, side="A")
    cur = admin._conn.cursor()
    try:
        cur.execute(f"create schema if not exists {SCHEMA}")
    finally:
        cur.close()
        admin.close()
    try:
        yield _with_schema(snowflake_url, SCHEMA)
    finally:
        admin = open_snowflake(snowflake_url, side="A")
        cur = admin._conn.cursor()
        try:
            cur.execute(f"drop schema if exists {SCHEMA} cascade")
            cur.execute(f"drop schema if exists {SCHEMA}_Q cascade")
        finally:
            cur.close()
            admin.close()


def _exec(url: str, *statements: str) -> list[Any]:
    """Run fixture DDL/DML through the driver (autocommit); last result."""
    d = open_snowflake(url, side="A")
    out: list[Any] = []
    try:
        for sql in statements:
            cur = d._conn.cursor()
            try:
                cur.execute(sql)
                out = list(cur.fetchall())
            finally:
                cur.close()
    finally:
        d.close()
    return out


@contextlib.contextmanager
def _user_default(params: dict[str, Any]) -> Iterator[None]:
    """Make the next login carry `params` as session parameters - where a
    user or account default (ALTER USER ... SET) would be in force."""
    original = sd.connect_kwargs

    def hostile(url, key):
        """The real connect kwargs with `params` merged into the session parameters."""
        kwargs = original(url, key)
        kwargs["session_parameters"] = {**kwargs["session_parameters"], **params}
        return kwargs

    sd.connect_kwargs = hostile
    try:
        yield
    finally:
        sd.connect_kwargs = original


def _open(url: str, side: str, params: dict[str, Any] | None = None, scale: int = 6):
    """Open a dialect, logging in with `params` as user defaults if given."""
    if not params:
        return open_snowflake(url, side=side, float_scale=scale)
    with _user_default(params):
        return open_snowflake(url, side=side, float_scale=scale)


def _diff(url, a_table, b_table, key, a=None, b=None, **kwargs):
    """Diff two tables on `url` and close both dialects."""
    a = a or _open(url, "A")
    b = b or _open(url, "B")
    try:
        return diff(a, b, a_table, b_table, key, **kwargs)
    finally:
        a.close()
        b.close()


def _found(result) -> dict[Any, tuple[str, list[str]]]:
    """The differences as {key: (kind, sorted columns)}."""
    return {d.key: (d.kind, sorted(d.columns)) for d in result.diffs}


def _pair(url: str, name: str, columns: str, rows_a: str, rows_b: str) -> None:
    """Tables <name>A and <name>B with the given rows (`select ...` text)."""
    _exec(
        url,
        f"create or replace table {name}A ({columns})",
        f"create or replace table {name}B ({columns})",
        f"insert into {name}A {rows_a}",
        f"insert into {name}B {rows_b}",
    )


# --- RT-HF1: VARIANT doubles ---------------------------------------------------


def test_rt_hf1_distinct_variant_doubles_are_different(sfh):
    """FALSE MATCH. Snowflake's TO_JSON writes a DOUBLE in a VARIANT as
    `d.ddddddddddddddde+xx` - 16 significant digits, where a double needs 17
    to round-trip. So 1 and 1.0000000000000002 (adjacent doubles), 0.3 and
    0.1 + 0.2, and the same inside an OBJECT all print alike, and every
    such row compares identical. The sample check reads the same TO_JSON
    text, so it agrees with the wrong answer. Verified live 2026-11-04."""
    _pair(
        sfh,
        "RTH_SF_VD",
        "id int, v variant",
        "select 1, parse_json('1e0') union all select 2, to_variant(0.3::float) "
        "union all select 3, parse_json('{\"x\": 2.5e0}') "
        "union all select 4, parse_json('[7.5e0]')",
        "select 1, parse_json('1.0000000000000002e0') "
        "union all select 2, to_variant(0.30000000000000004::float) "
        "union all select 3, parse_json('{\"x\": 2.5000000000000004e0}') "
        "union all select 4, parse_json('[7.5e0]')",
    )
    # The values really differ, by Snowflake's own equality.
    differ = _exec(
        sfh,
        "select a.id from RTH_SF_VDA a join RTH_SF_VDB b on a.id = b.id "
        "where a.v <> b.v order by 1",
    )
    assert [int(r[0]) for r in differ] == [1, 2, 3]
    # Snowflake cannot print such a double exactly, so the column is refused
    # - naming the side, the column and --exclude - never compared alike.
    with pytest.raises(ValueError, match=r"side B.*'V'.*double.*--exclude"):
        _diff(sfh, "RTH_SF_VDA", "RTH_SF_VDB", "id")
    # Left out, the rest of the table is still compared.
    result = _diff(sfh, "RTH_SF_VDA", "RTH_SF_VDB", "id", exclude=["v"])
    assert result.diffs == [] and "V" not in [c.upper() for c in result.columns]


def test_rt_hf1_variant_doubles_that_print_exactly_are_compared(sfh):
    """The other half of the RT-HF1 fix: a double whose 16-digit text reads
    back as itself is pinned down by it, so a document of such doubles is
    compared as before - and a planted change in one is found, at the top
    level and nested."""
    _pair(
        sfh,
        "RTH_SF_VE",
        "id int, v variant",
        "select 1, parse_json('1e0') union all select 2, to_variant(0.25::float) "
        'union all select 3, parse_json(\'{"x": [2.5e0, "s"]}\') '
        "union all select 4, parse_json('NaN')",
        "select 1, parse_json('1e0') union all select 2, to_variant(0.5::float) "
        'union all select 3, parse_json(\'{"x": [7.5e0, "s"]}\') '
        "union all select 4, parse_json('NaN')",
    )
    result = _diff(sfh, "RTH_SF_VEA", "RTH_SF_VEB", "id")
    assert _found(result) == {k: ("different", ["V"]) for k in (2, 3)}


# --- RT-HF2: QUOTED_IDENTIFIERS_IGNORE_CASE --------------------------------------


def _qiic_tables(url: str) -> None:
    """A quoted lower-case table holding the real rows, and an upper-case
    twin (a decoy) that matches side B. Under QUOTED_IDENTIFIERS_IGNORE_CASE,
    `"rth_sf_q"` names RTH_SF_Q."""
    _exec(
        url,
        'create or replace table "rth_sf_q" (id int, v varchar)',
        "create or replace table RTH_SF_Q (id int, v varchar)",
        "create or replace table RTH_SF_QB (id int, v varchar)",
        "insert into \"rth_sf_q\" values (1, 'real'), (2, 'decoy')",
        "insert into RTH_SF_Q values (1, 'decoy'), (2, 'decoy')",
        "insert into RTH_SF_QB values (1, 'decoy'), (2, 'decoy')",
    )


def test_rt_hf2_quoted_identifiers_ignore_case_reads_the_named_table(sfh):
    """FALSE MATCH. `columns()` finds `rth_sf_q` in information_schema (an
    exact string match), then every data query quotes it - and with
    QUOTED_IDENTIFIERS_IGNORE_CASE = TRUE as a user or account default, a
    quoted identifier is folded to upper case, so `"rth_sf_q"` resolves to
    the table RTH_SF_Q. The rows of the decoy are compared, and they match
    side B: identical, with row 1 really different. The dialect pins
    TIMEZONE and ROWS_PER_RESULTSET but not this. Verified live."""
    _qiic_tables(sfh)
    a = _open(sfh, "A", {"QUOTED_IDENTIFIERS_IGNORE_CASE": True})
    result = _diff(sfh, "rth_sf_q", "RTH_SF_QB", "id", a=a)
    assert _found(result) == {1: ("different", ["V"])}


def test_quoted_identifiers_ignore_case_on_the_schema_does_not_redirect(sfh):
    """The same parameter set on the *schema* (an object default, which any
    schema owner may set) does not change how the tool's session resolves
    `"rth_sf_q"`: the real table is read and row 1 is found."""
    q = _with_schema(sfh, SCHEMA + "_Q")
    _exec(sfh, f"create schema if not exists {SCHEMA}_Q")
    _exec(sfh, f"alter schema {SCHEMA}_Q set quoted_identifiers_ignore_case = true")
    _qiic_tables(q)
    result = _diff(q, "rth_sf_q", "RTH_SF_QB", "id")
    assert _found(result) == {1: ("different", ["V"])}


# --- RT-HF3: TIMESTAMP_TZ nanosecond keys ---------------------------------------


@pytest.fixture(scope="module")
def tz_keys(sfh) -> str:
    """Three TIMESTAMP_TZ(9)-keyed tables: same instant, same wall clock, both."""
    _exec(
        sfh,
        "create or replace table RTH_SF_TZA (k timestamp_tz(9), v int)",
        "create or replace table RTH_SF_TZB (k timestamp_tz(9), v int)",
        "create or replace table RTH_SF_TZC (k timestamp_tz(9), v int)",
        "insert into RTH_SF_TZA values ('2024-01-01 12:00:00.000000001 +00:00', 1), "
        "('2024-01-01 13:00:00 +00:00', 2)",
        # Same wall clock, five hours earlier as an instant.
        "insert into RTH_SF_TZB values ('2024-01-01 12:00:00.000000001 +05:00', 1), "
        "('2024-01-01 13:00:00 +00:00', 2)",
        # The very instant of A's first key, stored at +05:00.
        "insert into RTH_SF_TZC values ('2024-01-01 17:00:00.000000001 +05:00', 1), "
        "('2024-01-01 13:00:00 +00:00', 2)",
    )
    return sfh


def test_rt_hf3_distinct_nanosecond_tz_keys_do_not_match(tz_keys):
    """FALSE MATCH. `normalize_key` renders a key with a sub-microsecond part
    as `to_char(<raw column>, ...FF9)` - without the convert_timezone that
    `normalize` applies - so a TIMESTAMP_TZ key prints its stored wall clock
    and drops the offset. 12:00:00.000000001 +00:00 and +05:00 are instants
    five hours apart (Snowflake says they are not equal) and become one key:
    the rows pair up and the tables read identical. Verified live."""
    assert _exec(
        tz_keys,
        "select count(*) from RTH_SF_TZA a join RTH_SF_TZB b on a.k = b.k",
    ) == [(1,)]  # only the 13:00 key is the same instant
    result = _diff(tz_keys, "RTH_SF_TZA", "RTH_SF_TZB", "k")
    assert not result.identical
    assert sorted(kind for kind, _ in _found(result).values()) == [
        "only_in_a",
        "only_in_b",
    ]


def test_rt_hf3_one_nanosecond_instant_at_two_offsets_is_one_key(tz_keys):
    """False alarm, the other half of RT-HF3: the instant stored at +00:00
    and at +05:00 renders two wall clocks, reported only_in_a / only_in_b.
    The planted difference: none - so the result must be identical."""
    result = _diff(tz_keys, "RTH_SF_TZA", "RTH_SF_TZC", "k")
    assert result.diffs == [] and result.identical


# --- RT-HF4 / RT-HF5: masking and row access policies ----------------------------


@pytest.fixture(scope="module")
def policies(sfh) -> str:
    """Table pairs under a masking policy and a row access policy."""
    _pair(
        sfh,
        "RTH_SF_PM",
        "id int, v varchar",
        "select 1, 'a' union all select 2, 'b'",
        "select 1, 'a' union all select 2, 'DIFFERENT'",
    )
    _pair(
        sfh,
        "RTH_SF_PR",
        "id int, v varchar",
        "select 1, 'a' union all select 2, 'b'",
        "select 1, 'a' union all select 2, 'DIFFERENT'",
    )
    _exec(
        sfh,
        "create or replace masking policy RTH_SF_MASK as (val varchar) "
        "returns varchar -> '***'",
        "create or replace row access policy RTH_SF_RAP as (i int) "
        "returns boolean -> i < 2",
        "alter table RTH_SF_PMA modify column v set masking policy RTH_SF_MASK",
        "alter table RTH_SF_PMB modify column v set masking policy RTH_SF_MASK",
        "alter table RTH_SF_PRA add row access policy RTH_SF_RAP on (id)",
        "alter table RTH_SF_PRB add row access policy RTH_SF_RAP on (id)",
    )
    return sfh


def test_rt_hf4_a_masked_column_is_never_a_silent_match(policies):
    """FALSE MATCH. A masking policy makes both sides return `***` for the
    reading role, so row 2 ('b' against 'DIFFERENT') compares equal and the
    run reports identical with no word about it. PostgreSQL row-level
    security is named as a caveat (CLAUDE.md 4.11); a Snowflake masking
    policy - the same blindness - is not. Snowflake lists them in
    INFORMATION_SCHEMA.POLICY_REFERENCES. Verified live."""
    result = _diff(policies, "RTH_SF_PMA", "RTH_SF_PMB", "id")
    assert (not result.identical) or any("mask" in w.lower() for w in result.warnings)


def test_rt_hf5_a_row_access_policy_is_never_a_silent_match(policies):
    """FALSE MATCH. The row access policy shows the reading role only id < 2,
    so the differing row 2 is invisible on both sides: identical, silently."""
    result = _diff(policies, "RTH_SF_PRA", "RTH_SF_PRB", "id")
    assert (not result.identical) or any(
        "row access" in w.lower() for w in result.warnings
    )


@pytest.mark.parametrize(
    ("ddl", "message"),
    [
        (
            (
                "create or replace aggregation policy RTH_SF_AGP as () returns "
                "aggregation_constraint -> aggregation_constraint(min_group_size => 50)"
            ),
            "aggregation policy",
        ),
        (
            (
                "create or replace projection policy RTH_SF_PJP as () returns "
                "projection_constraint -> projection_constraint(allow => false)"
            ),
            "projection policy",
        ),
    ],
)
def test_aggregation_and_projection_policies_fail_loudly(sfh, ddl, message):
    """Unlike masking and row access policies, these two make Snowflake
    refuse the tool's queries outright, naming the side: no verdict at all,
    never a wrong one. (An aggregation policy fails on key_stats' MIN.)"""
    name = "RTH_SF_AG" if "aggregation" in ddl else "RTH_SF_PJ"
    _pair(
        sfh,
        name,
        "id int, v varchar",
        "select seq4(), 'x' from table(generator(rowcount => 200))",
        "select seq4(), case when seq4() = 77 then 'y' else 'x' end "
        "from table(generator(rowcount => 200))",
    )
    policy = "RTH_SF_AGP" if "aggregation" in ddl else "RTH_SF_PJP"
    attach = (
        "alter table {t} set aggregation policy RTH_SF_AGP"
        if "aggregation" in ddl
        else "alter table {t} modify column v set projection policy RTH_SF_PJP"
    )
    _exec(sfh, ddl, attach.format(t=name + "A"), attach.format(t=name + "B"))
    try:
        with pytest.raises(ValueError, match=rf"side A.*(?i:{message})"):
            _diff(sfh, name + "A", name + "B", "id")
    finally:
        _exec(sfh, f"drop table {name}A", f"drop table {name}B")
        _exec(sfh, f"drop {message} if exists {policy}")


# --- RT-HF6: STRICT_JSON_OUTPUT ---------------------------------------------------


def test_rt_hf6_strict_json_output_keeps_undefined_apart_from_null(sfh):
    """FALSE MATCH under a user default. ARRAY_CONSTRUCT(1, NULL) holds an
    SQL NULL element (`[1,undefined]`), PARSE_JSON('[1,null]') a JSON null;
    `v[1] is null` tells them apart, and without the setting the tool does.
    With STRICT_JSON_OUTPUT = TRUE, TO_JSON prints both as `[1,null]` and the
    row compares identical. Verified live; rows 1 and 3 (NaN, Infinity
    against null) are still found."""
    _pair(
        sfh,
        "RTH_SF_JS",
        "id int, v variant",
        "select 1, parse_json('NaN') union all select 2, array_construct(1, null) "
        "union all select 3, parse_json('Infinity')",
        "select 1, parse_json('null') union all select 2, parse_json('[1,null]') "
        "union all select 3, parse_json('null')",
    )
    control = _diff(sfh, "RTH_SF_JSA", "RTH_SF_JSB", "id")
    assert sorted(_found(control)) == [1, 2, 3]
    a = _open(sfh, "A", {"STRICT_JSON_OUTPUT": True})
    b = _open(sfh, "B", {"STRICT_JSON_OUTPUT": True})
    result = _diff(sfh, "RTH_SF_JSA", "RTH_SF_JSB", "id", a=a, b=b)
    assert sorted(_found(result)) == [1, 2, 3]


# --- RT-HF7: GEOGRAPHY / GEOMETRY / VECTOR -----------------------------------------


@pytest.mark.parametrize(
    ("typ", "same", "other"),
    [
        ("geography", "to_geography('POINT(1 2)')", "to_geography('POINT(1 2.000001)')"),
        ("geometry", "to_geometry('POINT(1 2)')", "to_geometry('POINT(1 2.000001)')"),
        ("vector(float, 3)", "[1,2,3]::vector(float,3)", "[1,2,3.5]::vector(float,3)"),
    ],
)
def test_rt_hf7_spatial_and_vector_columns_are_compared(sfh, typ, same, other):
    """Crash. These map to UNKNOWN, whose fallback is `cast(c as varchar)`,
    which Snowflake refuses for all three ("invalid type ... for parameter
    'TO_VARCHAR'") - the run dies in the first checksum query, after the
    key stats, naming neither the column nor `--exclude`. ST_ASWKB / a
    VECTOR's array cast would render them. Verified live."""
    name = "RTH_SF_X" + typ.split("(")[0].upper()[:3]
    _pair(
        sfh,
        name,
        f"id int, v {typ}",
        f"select 1, {same} union all select 2, {same}",
        f"select 1, {same} union all select 2, {other}",
    )
    result = _diff(sfh, name + "A", name + "B", "id")
    assert _found(result) == {2: ("different", ["V"])}


def test_decfloat_is_compared_as_its_exact_text(sfh):
    """DECFLOAT (unmapped, compared by its varchar cast, with the warning)
    keeps all 38 digits: a change in the last one is found."""
    _pair(
        sfh,
        "RTH_SF_DF",
        "id int, v decfloat",
        "select 1, '1.2345678901234567890123456789012345678'::decfloat "
        "union all select 2, '1e100'::decfloat",
        "select 1, '1.2345678901234567890123456789012345679'::decfloat "
        "union all select 2, '1e100'::decfloat",
    )
    result = _diff(sfh, "RTH_SF_DFA", "RTH_SF_DFB", "id")
    assert _found(result) == {1: ("different", ["V"])}
    assert any("unmapped" in w for w in result.warnings)


# --- RT-HF8 / RT-HF9: NUMBER(38) at its limits ------------------------------------


@pytest.mark.parametrize(
    ("typ", "wide", "scale"),
    [
        ("number(38,2)", "123456789012345678901234567890123456.78", 6),
        # At --float-scale 20 the cast is number(38,20): 18 integer digits.
        ("number(30,2)", "1234567890123456789.12", 20),
    ],
)
def test_rt_hf8_a_wide_number_value_is_compared(sfh, typ, wide, scale):
    """Crash. DECIMAL renders through `cast(c as number(38, 6))`, which holds
    32 integer digits; NUMBER(38,2) holds 36, so a legitimate value raises
    "Number out of representable range: FIXED(38,6)" and nothing can be
    compared (PostgreSQL's `round(numeric, 6)` renders it exactly). The
    error names the table but not the column. At --float-scale 20 the same
    happens from 10^18 in an ordinary NUMBER(30,2). Verified live."""
    _pair(
        sfh,
        "RTH_SF_NW",
        f"id int, w {typ}",
        f"select 1, {wide} union all select 2, 1.00",
        f"select 1, {wide} union all select 2, 1.01",
    )
    result = _diff(
        sfh,
        "RTH_SF_NWA",
        "RTH_SF_NWB",
        "id",
        a=_open(sfh, "A", scale=scale),
        b=_open(sfh, "B", scale=scale),
    )
    assert _found(result) == {2: ("different", ["W"])}


def test_number_38_37_is_compared_at_the_scale(sfh):
    """NUMBER(38,37) rounds half away from zero to six places, like every
    other engine: 0.0000005 is 0.000001, 0.00000049999... is 0.000000."""
    _pair(
        sfh,
        "RTH_SF_NF",
        "id int, f number(38,37)",
        "select 1, 0.1234567890123456789012345678901234567 union all select 2, 0.0000005",
        "select 1, 0.1234567890123456789012345678901234567 "
        "union all select 2, 0.0000004999999999999999999999999999999",
    )
    result = _diff(sfh, "RTH_SF_NFA", "RTH_SF_NFB", "id")
    assert _found(result) == {2: ("different", ["F"])}


@pytest.mark.parametrize("low", ["0", "-" + "9" * 38])
def test_rt_hf9_number_38_keys_across_a_wide_range(sfh, low):
    """Crash. The bucket expression multiplies the key offset by the number
    of segments inside NUMBER(38,0), so a span past 10^38 / 32 overflows
    ("Number out of representable range ... value 3.2e+38" for keys 0..10^37),
    and a span past 10^38 is refused as a literal ("Integer literal is out of
    representable range"). Keys 0 to 10^36 work. NUMBER(38,0) keys of 37 and
    38 digits are legitimate data. Verified live."""
    big = "9" * 38 if low != "0" else "1" + "0" * 37
    _pair(
        sfh,
        "RTH_SF_NK",
        "k number(38,0), v int",
        f"select {low}, 1 union all select 5, 2 union all select {big}, 3",
        f"select {low}, 1 union all select 5, 20 union all select {big}, 3",
    )
    result = _diff(sfh, "RTH_SF_NKA", "RTH_SF_NKB", "k")
    assert _found(result) == {5: ("different", ["V"])}


# --- regressions that hold ----------------------------------------------------------


@pytest.mark.parametrize(
    "collation", ["en-ci", "en-ci-rtrim", "upper", "utf8", "de_DE-ci-ai"]
)
def test_collated_keys_and_values_compare_by_bytes(sfh, collation):
    """Keys 'a', 'A' and 'a ' are three keys under any collation, and values
    differing only by case, a trailing space or an accent are differences.
    Verified live for five collations (the key identity is escaped through
    REPLACE, which drops the collation, so `count(distinct ...)` is exact)."""
    name = "RTH_SF_C" + collation.replace("-", "_").upper()
    cols = f"k varchar collate '{collation}', v varchar collate '{collation}'"
    _pair(
        sfh,
        name,
        cols,
        "select * from values ('a','x'),('A','x'),('a ','x'),('b','same'),('c','Ä')",
        "select * from values ('a','x'),('A','X'),('a ','x'),('b','same '),('c','A')",
    )
    result = _diff(sfh, name + "A", name + "B", "k")
    assert _found(result) == {k: ("different", ["V"]) for k in ("A", "b", "c")}


#: User defaults that could change rendering, row visibility or the queries.
#: Each was run live against a mixed-type table with one planted difference
#: per column; the verdict was the same as without it. (QUOTED_IDENTIFIERS_
#: IGNORE_CASE and STRICT_JSON_OUTPUT do change it - RT-HF2, RT-HF6 - but not
#: on this table.)
HOSTILE = [
    {"TIMESTAMP_OUTPUT_FORMAT": "YYYY"},
    {"TIMESTAMP_NTZ_OUTPUT_FORMAT": "YYYY-MM-DD HH24:MI:SS.FF3"},
    {"TIMESTAMP_LTZ_OUTPUT_FORMAT": "DY"},
    {"TIMESTAMP_TZ_OUTPUT_FORMAT": "HH24"},
    {"TIME_OUTPUT_FORMAT": "HH24"},
    {"DATE_OUTPUT_FORMAT": "YYYY"},
    {"BINARY_OUTPUT_FORMAT": "BASE64"},
    {"TIMESTAMP_TYPE_MAPPING": "TIMESTAMP_TZ"},
    {"TIMEZONE": "Asia/Kathmandu"},
    {"ROWS_PER_RESULTSET": 1},
    {"PYTHON_CONNECTOR_QUERY_RESULT_FORMAT": "JSON"},
    {"WEEK_START": 3, "WEEK_OF_YEAR_POLICY": 1, "JS_TREAT_INTEGER_AS_BIGINT": True},
    {"ERROR_ON_NONDETERMINISTIC_UPDATE": True, "ERROR_ON_NONDETERMINISTIC_MERGE": False},
    {"AUTOCOMMIT": False, "TRANSACTION_ABORT_ON_ERROR": True, "MULTI_STATEMENT_COUNT": 0},
    {"DATE_INPUT_FORMAT": "DD/MM/YYYY", "TIMESTAMP_INPUT_FORMAT": "DD/MM/YYYY"},
    {"BINARY_INPUT_FORMAT": "BASE64", "TIME_INPUT_FORMAT": "HH24MI"},
    {"ODBC_TREAT_DECIMAL_AS_INT": True, "JSON_INDENT": 8, "USE_CACHED_RESULT": False},
    {"STATEMENT_TIMEOUT_IN_SECONDS": 60, "LOCK_TIMEOUT": 1},
]

_MIXED_COLS = (
    "id int, n number(12,2), f float, b boolean, s varchar, d date, tm time(9), "
    "ntz timestamp_ntz(9), ltz timestamp_ltz(9), tz timestamp_tz(9), bi binary, va variant"
)
_MIXED_ROW = (
    "select {i}, 12.34, 0.1, true, 'text é ' || {i}, '2024-02-29'::date, "
    "'12:34:56.123456789'::time(9), '2024-01-01 01:02:03.123456789'::timestamp_ntz(9), "
    "'2024-01-01 01:02:03.123456789 +00:00'::timestamp_ltz(9), "
    "'2024-01-01 01:02:03.123456789 +03:00'::timestamp_tz(9), "
    "to_binary('00ff10', 'HEX'), parse_json('{{\"a\":[1,\"1\",1.5]}}')"
)
_MIXED_PLANTS = {
    1: ("n", "12.35"),
    2: ("f", "0.1000011"),
    3: ("b", "null"),
    4: ("s", "'text e 4'"),
    5: ("d", "'2024-03-01'"),
    6: ("tm", "'12:34:56.123456788'::time(9)"),
    7: ("ntz", "'2024-01-01 01:02:03.123456788'"),
    8: ("ltz", "'2024-01-01 01:02:03.123457789 +00:00'"),
    9: ("tz", "'2024-01-01 01:02:03.123456789 +04:00'"),
    10: ("bi", "to_binary('00FF11', 'HEX')"),
    11: ("va", "parse_json('{\"a\":[1,1,1.5]}')"),
}


@pytest.fixture(scope="module")
def mixed(sfh) -> str:
    """A mixed-type table pair with one planted change per row in `_MIXED_PLANTS`."""
    rows = " union all ".join(_MIXED_ROW.format(i=i) for i in range(1, 15))
    _pair(sfh, "RTH_SF_MX", _MIXED_COLS, rows, rows)
    _exec(
        sfh,
        *(
            f"update RTH_SF_MXB set {c} = {v} where id = {i}"
            for i, (c, v) in _MIXED_PLANTS.items()
        ),
    )
    return sfh


@pytest.mark.parametrize("params", HOSTILE, ids=lambda p: "+".join(p))
def test_a_hostile_user_default_does_not_change_the_verdict(mixed, params):
    """Hostile user-default session parameters change no difference found."""
    a = _open(mixed, "A", params)
    result = _diff(mixed, "RTH_SF_MXA", "RTH_SF_MXB", "id", a=a)
    assert _found(result) == {
        i: ("different", [c.upper()]) for i, (c, _) in _MIXED_PLANTS.items()
    }
    assert not result.unexplained


def test_a_udf_named_like_a_builtin_does_not_shadow_it(sfh):
    """Built-ins win over a same-named UDF in the current schema (unlike
    PostgreSQL's search_path, which the dialect there must pin): a schema
    holding MD5_NUMBER_UPPER64 and REPLACE functions that would make every
    row hash alike still has its planted difference found."""
    _pair(
        sfh,
        "RTH_SF_UF",
        "id int, v varchar",
        "select 1, 'a' union all select 2, 'b'",
        "select 1, 'a' union all select 2, 'c'",
    )
    _exec(
        sfh,
        f"create or replace function {SCHEMA}.MD5_NUMBER_UPPER64(x varchar) returns number as '16'",
        f"create or replace function {SCHEMA}.REPLACE(x varchar, y varchar, z varchar) "
        "returns varchar as '''same'''",
    )
    try:
        result = _diff(sfh, "RTH_SF_UFA", "RTH_SF_UFB", "id")
    finally:
        _exec(
            sfh,
            f"drop function if exists {SCHEMA}.MD5_NUMBER_UPPER64(varchar)",
            f"drop function if exists {SCHEMA}.REPLACE(varchar, varchar, varchar)",
        )
    assert _found(result) == {2: ("different", ["V"])}


def test_far_dates_and_years_render_in_full(sfh):
    """Year 12024 is not 2024, and years 0 and -43 (astronomical) round-trip:
    to_char prints every digit of the year, so the planted date and the
    planted second are found and nothing else."""
    a_rows = (
        "select 1, date_from_parts(12024,1,2), timestamp_ntz_from_parts(12024,1,2,0,0,0) "
        "union all select 2, date_from_parts(0,1,1), timestamp_ntz_from_parts(0,1,1,0,0,0) "
        "union all select 3, date_from_parts(-43,3,15), timestamp_ntz_from_parts(-43,3,15,0,0,0)"
    )
    b_rows = a_rows.replace("date_from_parts(12024", "date_from_parts(2024").replace(
        "(-43,3,15,0,0,0)", "(-43,3,15,0,0,1)"
    )
    _pair(sfh, "RTH_SF_FD", "id int, d date, t timestamp_ntz", a_rows, b_rows)
    result = _diff(sfh, "RTH_SF_FDA", "RTH_SF_FDB", "id")
    assert _found(result) == {1: ("different", ["D"]), 3: ("different", ["T"])}


# --- live tables -------------------------------------------------------------------


@pytest.fixture()
def live_pair(sfh) -> str:
    """A 2,000-row table and a clone with row 777 changed."""
    _exec(
        sfh,
        "create or replace table RTH_SF_LA (id int, v varchar)",
        "insert into RTH_SF_LA select seq4(), 'x' from table(generator(rowcount => 2000))",
        "create or replace table RTH_SF_LB clone RTH_SF_LA",
        "update RTH_SF_LB set v = 'y' where id = 777",
    )
    return sfh


def _mid_walk(url: str, statement: str):
    """Diff with `statement` run from another connection right after side
    A's first checksum query (the first that hashes rows)."""
    a = _open(url, "A")
    original = a.query
    fired = {"done": False}

    def hooked(sql: str):
        """Run the query; after side A's first checksum, run `statement` once."""
        rows = original(sql)
        if not fired["done"] and "md5_number_upper64" in sql:
            fired["done"] = True
            _exec(url, statement)
        return rows

    a.query = hooked  # type: ignore[method-assign]
    return _diff(
        url, "RTH_SF_LA", "RTH_SF_LB", "id", a=a, threshold=16, bisection_factor=4
    )


@pytest.mark.sample_check_may_disagree
def test_a_difference_fixed_mid_walk_is_never_identical(live_pair):
    """READ COMMITTED: the difference at 777 is repaired on side A after the
    first checksum. The walk must not then say identical - the per-level sum
    check reads the range as unexplained."""
    result = _mid_walk(live_pair, "update RTH_SF_LA set v = 'y' where id = 777")
    assert not result.identical
    assert result.unexplained or result.diffs


@pytest.mark.sample_check_may_disagree
def test_a_row_inserted_mid_walk_does_not_hide_the_planted_difference(live_pair):
    """A row inserted past the key range after the first checksum: the walk
    (bounded by the range it read first) still finds 777, and the sample
    check, which reads later, names the new key - verified live."""
    result = _mid_walk(live_pair, "insert into RTH_SF_LA values (5000, 'z')")
    assert _found(result).get(777) == ("different", ["V"])
    assert not result.identical
    assert result.verification is not None
    assert any("5000" in f for f in result.verification.failures)


@pytest.mark.sample_check_may_disagree
def test_a_crafted_double_slash_filter_is_caught_downstream(sfh):
    """RT-HF10's crafted predicate escapes the parentheses on Snowflake; the
    per-level row-count check still keeps the verdict from being identical.
    Differences planted at 30 (inside the filter) and 45 (outside). The guard
    now refuses the predicate, so it is switched off here to keep the second
    line of defence tested."""
    _exec(
        sfh,
        "create or replace table RTH_SF_WA (id int, v varchar)",
        "insert into RTH_SF_WA select seq4(), 'x' from table(generator(rowcount => 100))",
        "create or replace table RTH_SF_WB clone RTH_SF_WA",
        "update RTH_SF_WB set v = 'CHANGED' where id in (30, 45)",
    )
    p = "id < 40 // (\n) or (1=0 // )\n or 1=0"
    a, b = _open(sfh, "A"), _open(sfh, "B")
    for d in (a, b):
        d.check_where = lambda where: None  # type: ignore[method-assign]
    result = _diff(
        sfh,
        "RTH_SF_WA",
        "RTH_SF_WB",
        "id",
        a=a,
        b=b,
        where_a=p,
        where_b=p,
        threshold=4,
        bisection_factor=2,
    )
    assert not result.identical
    assert result.unexplained


# --- object kinds, shapes, names ----------------------------------------------------


@pytest.fixture(scope="module")
def objects(sfh) -> str:
    """A table and its changed clone, as table, view, secure view and transient."""
    _exec(
        sfh,
        "create or replace table RTH_SF_OA (id int, v varchar, w int)",
        "insert into RTH_SF_OA select seq4(), 'v' || seq4(), seq4() "
        "from table(generator(rowcount => 500))",
        "create or replace table RTH_SF_OB clone RTH_SF_OA",
        "update RTH_SF_OB set v = 'changed' where id = 123",
        "create or replace view RTH_SF_OVW as select * from RTH_SF_OB",
        "create or replace secure view RTH_SF_OSV as select * from RTH_SF_OB",
        "create or replace transient table RTH_SF_OTR clone RTH_SF_OB",
    )
    return sfh


@pytest.mark.parametrize(
    "b_table", ["RTH_SF_OB", "RTH_SF_OVW", "RTH_SF_OSV", "RTH_SF_OTR"]
)
def test_views_secure_views_clones_and_transient_tables(objects, b_table):
    """Views, secure views, clones and transient tables find the one change."""
    result = _diff(objects, "RTH_SF_OA", b_table, "id")
    assert _found(result) == {123: ("different", ["V"])}


def test_materialized_and_dynamic_tables(objects):
    """Enterprise objects (the trial has them): verified live; dropped at once
    so neither keeps refreshing on the account's credits."""
    _exec(
        objects,
        "create or replace materialized view RTH_SF_OMV as select id, v, w from RTH_SF_OB",
        "create or replace dynamic table RTH_SF_ODT target_lag = '7 days' "
        "warehouse = COMPUTE_WH as select * from RTH_SF_OB",
    )
    try:
        for b_table in ("RTH_SF_OMV", "RTH_SF_ODT"):
            result = _diff(objects, "RTH_SF_OA", b_table, "id")
            assert _found(result) == {123: ("different", ["V"])}, b_table
    finally:
        _exec(
            objects,
            "drop dynamic table if exists RTH_SF_ODT",
            "drop materialized view if exists RTH_SF_OMV",
        )


def test_a_600_column_table_finds_a_change_in_the_last_column(sfh):
    """A 600-column table finds a change in its last column."""
    cols = ", ".join(f"c{i} int" for i in range(600))
    sel = ", ".join(f"seq4() + {i}" for i in range(600))
    _exec(
        sfh,
        f"create or replace table RTH_SF_WIA (id int, {cols})",
        f"insert into RTH_SF_WIA select seq4(), {sel} from table(generator(rowcount => 50))",
        "create or replace table RTH_SF_WIB clone RTH_SF_WIA",
        "update RTH_SF_WIB set c599 = -1 where id = 7",
    )
    result = _diff(sfh, "RTH_SF_WIA", "RTH_SF_WIB", "id")
    assert _found(result) == {7: ("different", ["C599"])}


def test_odd_quoted_column_names(sfh):
    """Spaces, a doubled quote, a reserved word, Unicode, lower and mixed
    case, a dot: all quoted back exactly as stored."""
    odd = ['"sp ace"', '"q""uote"', '"select"', '"Ünï ☃"', '"lower"', '"MiXeD"', '"a.b"']
    _exec(
        sfh,
        'create or replace table "rth_sf_odd" ("k ey" int, '
        + ", ".join(f"{c} int" for c in odd)
        + ")",
        'insert into "rth_sf_odd" select seq4(), 1, 2, 3, 4, 5, 6, 7 '
        "from table(generator(rowcount => 20))",
        'create or replace table "rth_sf_odd2" clone "rth_sf_odd"',
        'update "rth_sf_odd2" set "Ünï ☃" = 99 where "k ey" = 3',
        'update "rth_sf_odd2" set "q""uote" = 99 where "k ey" = 4',
    )
    result = _diff(sfh, "rth_sf_odd", "rth_sf_odd2", "k ey")
    assert _found(result) == {3: ("different", ["Ünï ☃"]), 4: ("different", ['q"uote'])}


def test_timestamp_tz_at_every_kind_of_offset(sfh):
    """+14:00, -12:00, +05:45, -00:30 and +00:00, the same instants restored
    at New York's offset on side B: equal; one minute moved: found."""
    offs = ["+14:00", "-12:00", "+05:45", "-00:30", "+00:00"]
    vals = ", ".join(
        f"({i}, '2024-03-10 02:30:00.123456 {o}')" for i, o in enumerate(offs)
    )
    _exec(
        sfh,
        "create or replace table RTH_SF_OFA (id int, t timestamp_tz(6))",
        f"insert into RTH_SF_OFA values {vals}",
        "create or replace table RTH_SF_OFB (id int, t timestamp_tz(6))",
        "insert into RTH_SF_OFB select id, convert_timezone('America/New_York', t) "
        "from RTH_SF_OFA",
        "update RTH_SF_OFB set t = '2024-03-10 02:30:00.123456 +05:46' where id = 2",
    )
    result = _diff(sfh, "RTH_SF_OFA", "RTH_SF_OFB", "id")
    assert _found(result) == {2: ("different", ["T"])}


def test_nul_separator_and_sentinel_lookalikes_in_values(sfh):
    """A trailing NUL, an inner NUL, chr(31) against chr(30), and the four
    bytes `\\N` against SQL NULL: each is a difference; the control row is not."""
    _pair(
        sfh,
        "RTH_SF_NU",
        "id int, v varchar",
        "select 1, 'a' || chr(0) union all select 2, 'a' || chr(0) || 'b' "
        "union all select 3, chr(31) || 'x' union all select 4, chr(92) || 'N' "
        "union all select 5, 'same'",
        "select 1, 'a' union all select 2, 'ab' union all select 3, chr(30) || 'x' "
        "union all select 4, null union all select 5, 'same'",
    )
    result = _diff(sfh, "RTH_SF_NUA", "RTH_SF_NUB", "id")
    assert _found(result) == {k: ("different", ["V"]) for k in (1, 2, 3, 4)}


@pytest.mark.parametrize(
    ("typ", "keys", "planted"),
    [
        (
            "binary",
            ["to_binary('00','HEX')", "to_binary('0000','HEX')", "to_binary('FF','HEX')"],
            1,
        ),
        ("date", ["'2024-01-01'::date", "'0001-01-01'::date", "'9999-12-31'::date"], 1),
        (
            "time(9)",
            [
                "'00:00:00'::time(9)",
                "'00:00:00.000000001'::time(9)",
                "'23:59:59.999999999'::time(9)",
            ],
            1,
        ),
        ("boolean", ["true", "false"], 1),
        ("variant", ["parse_json('1')", "parse_json('\"1\"')", "parse_json('1.5')"], 1),
        (
            "timestamp_ltz(9)",
            [
                "'2024-01-01 00:00:00.000000001 +02:00'::timestamp_ltz(9)",
                "'2024-01-01 00:00:00 +00:00'::timestamp_ltz(9)",
            ],
            1,
        ),
        ("varchar", ["'a'", "'a' || chr(31)", "chr(92) || 'N'", "''"], 2),
    ],
)
def test_every_key_type_finds_the_planted_row(sfh, typ, keys, planted):
    """Keys of every non-integer type that is allowed: BINARY 00 and 0000,
    TIME and TIMESTAMP_LTZ nanoseconds, the number 1 and the string "1", a
    key ending in the separator or equal to the NULL sentinel's bytes."""
    name = "RTH_SF_K" + typ.split("(")[0].upper()[:6]
    rows_a = " union all ".join(f"select {k}, {i}" for i, k in enumerate(keys))
    rows_b = " union all ".join(
        f"select {k}, {i + 100 if i == planted else i}" for i, k in enumerate(keys)
    )
    _pair(sfh, name, f"k {typ}, v int", rows_a, rows_b)
    result = _diff(sfh, name + "A", name + "B", "k")
    assert [(kind, cols) for kind, cols in _found(result).values()] == [
        ("different", ["V"])
    ]


def test_a_float_key_is_refused_naming_side_and_column(sfh):
    """A FLOAT key is refused naming the side and the column."""
    _pair(sfh, "RTH_SF_KF", "k float, v int", "select 1.5, 1", "select 1.5, 2")
    with pytest.raises(ValueError, match=r"side A.*'K' cannot be used as a key"):
        _diff(sfh, "RTH_SF_KFA", "RTH_SF_KFB", "k")


def test_a_composite_key_with_a_separator_in_its_text(sfh):
    """('x' || chr(31), 1), ('x', 1) and ('x\\', 1) are three keys; the
    planted change on ('x', 1) is the only difference."""
    _pair(
        sfh,
        "RTH_SF_CK",
        "a varchar, b int, v int",
        "select 'x' || chr(31), 1, 1 union all select 'x', 1, 2 "
        "union all select 'x' || chr(92), 1, 3",
        "select 'x' || chr(31), 1, 1 union all select 'x', 1, 20 "
        "union all select 'x' || chr(92), 1, 3",
    )
    result = _diff(sfh, "RTH_SF_CKA", "RTH_SF_CKB", ["a", "b"])
    assert [(kind, cols) for kind, cols in _found(result).values()] == [
        ("different", ["V"])
    ]


@pytest.fixture(scope="module")
def where_pair(sfh) -> str:
    """A 300-row pair with quotes and backslashes, rows 3, 4 and 200 changed."""
    _exec(
        sfh,
        "create or replace table RTH_SF_W2A (id int, v varchar)",
        "insert into RTH_SF_W2A select seq4(), case when seq4() % 3 = 0 then 'it''s' "
        "when seq4() % 3 = 1 then 'a' || chr(92) || 'b' else 'x' end "
        "from table(generator(rowcount => 300))",
        "create or replace table RTH_SF_W2B clone RTH_SF_W2A",
        "update RTH_SF_W2B set v = 'CH' where id in (3, 4, 200)",
    )
    return sfh


@pytest.mark.parametrize(
    ("where", "want"),
    [
        ("identifier('ID') < 100", {3: "different", 4: "different"}),
        ('"ID" < 100', {3: "different", 4: "different"}),
        ("v = 'it''s'", {3: "only_in_a"}),
        ("v = 'a\\\\b'", {4: "only_in_a"}),  # Snowflake reads '\\' as one backslash
        (
            "id in (select id from RTH_SF_W2A where id < 100)",
            {3: "different", 4: "different"},
        ),
        ("v <> 'http://x'", {3: "different", 4: "different", 200: "different"}),
        ("array_contains(id::variant, [3, 4])", {3: "different", 4: "different"}),
    ],
)
def test_snowflake_row_filters_select_exactly_their_rows(where_pair, where, want):
    """Each Snowflake-spelled row filter selects exactly its rows on both sides."""
    result = _diff(
        where_pair, "RTH_SF_W2A", "RTH_SF_W2B", "id", where_a=where, where_b=where
    )
    assert {k: kind for k, (kind, _) in _found(result).items()} == want


@pytest.mark.parametrize(
    "where",
    [
        "$$x$$ = 'x'",
        "v = 'it\\'s'",
        "id < 100 /* c */",
        "id < 100 -- c",
        "id < 100; select 1",
    ],
)
def test_snowflake_quoting_tricks_are_refused_before_any_query(where):
    """Dollar quotes, backslash escapes, comments and `;` are refused offline."""
    with pytest.raises(ValueError, match="row filter"):
        SnowflakeDialect(side="A").check_where(where)


@pytest.mark.sample_check_may_disagree
def test_a_random_row_filter_is_never_identical(where_pair):
    """A predicate that changes between queries: the per-level counts catch
    it (unexplained), whatever rows it happened to pick."""
    w = "uniform(0::float, 1::float, random()) < 0.5"
    result = _diff(where_pair, "RTH_SF_W2A", "RTH_SF_W2B", "id", where_a=w, where_b=w)
    assert not result.identical and result.unexplained


def test_map_pairs_a_renamed_mixed_case_column(sfh):
    """`column_map` pairs a column renamed to a quoted mixed-case name."""
    _exec(
        sfh,
        "create or replace table RTH_SF_MPA (id int, amount int, note varchar)",
        "insert into RTH_SF_MPA select seq4(), seq4(), 'n' from table(generator(rowcount => 50))",
        'create or replace table RTH_SF_MPB (id int, "amount_New" int, NOTE varchar)',
        "insert into RTH_SF_MPB select id, amount, note from RTH_SF_MPA",
        'update RTH_SF_MPB set "amount_New" = -1 where id = 9',
    )
    result = _diff(
        sfh, "RTH_SF_MPA", "RTH_SF_MPB", "id", column_map={"amount": "amount_New"}
    )
    assert _found(result) == {9: ("different", ["AMOUNT"])}
    assert not result.schema_differences


_SPECIAL_FLOATS = [
    "0.0", "-0.0", "'nan'", "'inf'", "'-inf'", "5e-324", "2.2250738585072014e-308",
    "9007199254740992", "9007199254740993", "4503599627370496.5", "1e15+0.5", "0.1",
    "1e34", "-123456.0000005", "2.0000005",
]  # fmt: skip
_SPECIAL_TEXT = [
    "", " ", "a" + chr(0x301), chr(0xE1), "\U0001f600", "שלום",
    "\U00010348", chr(0xFEFF), "tab\there", "\\", "\\\\N", chr(0x2028),
]  # fmt: skip


def test_specials_against_duckdb(sfh, tmp_path):
    """Snowflake against DuckDB: -0, NaN, +-Infinity, subnormals, 2^53 and
    its neighbours, a 1e15 + 0.5 tie, combining marks against precomposed,
    astral and RTL text, a BOM, U+2028, backslashes, TIME(9) (truncated to
    microseconds, with the caveat), TIMESTAMP_NTZ(3) and TIMESTAMP_TZ at
    nine offsets. One planted text change is the only difference."""
    from conftest import duckdb_write, open_duckdb

    rows = []
    for i, f in enumerate(_SPECIAL_FLOATS):
        s = _SPECIAL_TEXT[i % len(_SPECIAL_TEXT)].replace("'", "''").replace("\\", "\\\\")
        rows.append(
            f"select {i}, {f}::float, '{s}', '12:34:56.{i:09d}'::time(9), "
            f"'2024-01-01 00:00:0{i % 10}.{i:03d}'::timestamp_ntz(3), "
            f"'2024-06-0{1 + i % 9} 10:00:00.{i:06d} +0{i % 10}:00'::timestamp_tz(6)"
        )
    _exec(
        sfh,
        "create or replace table RTH_SF_XA (id int, f float, s varchar, t time(9), "
        "ts timestamp_ntz(3), tz timestamp_tz(6))",
        "insert into RTH_SF_XA " + " union all ".join(rows),
    )
    path = str(tmp_path / "x.duckdb")
    con = duckdb_write(path)
    try:
        con.execute("set TimeZone='UTC'")
        con.execute(
            "create table x (id bigint, f double, s varchar, t time, ts timestamp, "
            "tz timestamptz)"
        )
        for i, f in enumerate(_SPECIAL_FLOATS):
            ff = {"'nan'": "'nan'::double", "'inf'": "'inf'::double"}.get(f, f)
            ff = "'-inf'::double" if f == "'-inf'" else ff
            con.execute(
                f"insert into x values (?, {ff}, ?, ?::time, ?::timestamp, ?::timestamptz)",
                [
                    i,
                    _SPECIAL_TEXT[i % len(_SPECIAL_TEXT)],
                    f"12:34:56.{i:09d}"[:15],
                    f"2024-01-01 00:00:0{i % 10}.{i:03d}",
                    f"2024-06-0{1 + i % 9} 10:00:00.{i:06d}+0{i % 10}:00",
                ],
            )
        con.execute("update x set s = s || '!' where id = 3")
    finally:
        con.close()
    a = _open(sfh, "A")
    b = open_duckdb(path, side="B")
    try:
        result = diff(a, b, "RTH_SF_XA", "main.x", "id")
    finally:
        a.close()
        b.close()
    assert _found(result) == {3: ("different", ["S"])}
    assert any("nanoseconds" in w for w in result.warnings)


# --- CLI end to end ------------------------------------------------------------------


def _cli(*argv: str) -> tuple[int, str]:
    """Run the CLI in-process; return its exit code and all output."""
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue() + err.getvalue()


def test_cli_json_finds_the_planted_difference_and_prints_no_secret(objects):
    """The CLI's JSON names the planted difference and never the password."""
    password = urlsplit(objects).password or ""
    code, text = _cli(
        "diff", "--a", objects, "--a-table", "RTH_SF_OA",
        "--b", objects, "--b-table", "RTH_SF_OB", "--key", "id", "--json",
    )  # fmt: skip
    assert code == 1, "exit code"
    report = json.loads(text)
    assert [(d["key"], d["columns"]) for d in report["differences"]] == [(123, ["V"])]
    # Computed apart, so a failing assertion cannot print the password.
    leaked = not password or password in text
    assert not leaked, "the password reached the output"


def test_cli_a_failed_login_exits_2_without_the_password(sf_url):
    """A user that does not exist (so no real login is ever locked out) and a
    password that is unmistakable: the error must not carry it."""
    p = urlsplit(sf_url)
    secret = f"Rth-{uuid.uuid4().hex}-secret"
    host = p.netloc.rsplit("@", 1)[1]
    bad = urlunsplit((p.scheme, f"RTH_NO_SUCH_USER:{secret}@{host}", p.path, p.query, ""))
    code, text = _cli(
        "diff", "--a", bad, "--a-table", "T", "--b", bad, "--b-table", "T", "--key", "id",
    )  # fmt: skip
    # Booleans first: a failing assertion must not print the output, which
    # names the account.
    names_side, leaked = "side A" in text, secret in text
    assert code == 2
    assert names_side
    assert not leaked


def test_cli_an_unknown_url_parameter_is_refused_without_its_value(sf_url):
    """An unknown URL parameter is refused by name, its value never printed."""
    secret = f"Rth-{uuid.uuid4().hex}"
    bad = sf_url + ("&" if "?" in sf_url else "?") + f"passcode={secret}"
    code, text = _cli(
        "diff", "--a", bad, "--a-table", "T", "--b", bad, "--b-table", "T", "--key", "id",
    )  # fmt: skip
    named, leaked_value = "passcode" in text, secret in text
    assert code == 2
    assert named and not leaked_value
    password = urlsplit(sf_url).password or ""
    leaked = not password or password in text
    assert not leaked, "the password reached the output"
