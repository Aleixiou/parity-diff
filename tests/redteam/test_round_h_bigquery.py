"""Red-team round H: BigQuery, live.

Every test plants a difference and asserts exactly it is found, or asserts a
refusal. A confirmed bug is a strict xfail named RT-HB<n>, so it turns green
(and then fails as XPASS) the moment it is fixed.

Cost discipline (BigQuery is billed): tables hold a handful of rows, value
probes run over literals (`select ... from (select <literal> as c)`), which
reference no table and bill nothing, and every table this module creates is
prefixed `rth_bq_`, suffixed per run and deleted at module teardown.

Skips cleanly unless PARITY_TEST_BIGQUERY points at a reachable project;
the PostgreSQL cross-checks also need the local server (PARITY_TEST_PG).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

import pytest
from conftest import PG_URL, duckdb_write, open_bigquery, open_duckdb, open_pg

from parity.dialects.base import canonical_float_text, row_filter_problem
from parity.dialects.bigquery_dialect import BigQueryDialect, _column, parse_bigquery_url
from parity.engine import diff

pytestmark = pytest.mark.bigquery

RUN = uuid.uuid4().hex[:8]


# ------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module")
def bq(bigquery_url):
    """(run, name, ds): a read-write client for fixtures, and the cleanup.

    `name(base)` hands out `rth_bq_<base>_<run>` and remembers it; everything
    remembered is deleted at the end, tables, views and snapshots alike.
    """
    from google.cloud import bigquery

    project, dataset, location = parse_bigquery_url(bigquery_url)
    client = bigquery.Client(project=project, location=location)
    made: list[str] = []

    def run(sql: str) -> list[tuple]:
        return [tuple(r.values()) for r in client.query_and_wait(sql)]

    def name(base: str) -> str:
        n = f"rth_bq_{base}_{RUN}"
        made.append(n)
        return n

    ds = f"`{project}`.`{dataset}`"
    yield run, name, ds, client
    for n in reversed(made):
        client.delete_table(f"{project}.{dataset}.{n}", not_found_ok=True)
    client.close()


@pytest.fixture
def pgw():
    """A read-write PostgreSQL connection for fixtures, or skip."""
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(PG_URL, autocommit=True, connect_timeout=5)
    except Exception as exc:  # pragma: no cover - depends on the machine
        pytest.skip(f"no PostgreSQL: {type(exc).__name__}")
    made: list[str] = []

    class W:
        def table(self, base: str, ddl: str) -> str:
            t = f"rth_bq_{base}_{RUN}"
            conn.execute(f"drop table if exists {t}")
            conn.execute(f"create table {t} ({ddl})")
            made.append(t)
            return t

        def execute(self, sql: str, params=None):
            return conn.execute(sql, params)

    yield W()
    for t in made:
        conn.execute(f"drop table if exists {t}")
    conn.close()


def _duck(tmp_path, *sql: str) -> str:
    """A DuckDB file holding the tables `sql` builds; closed before reading."""
    path = str(tmp_path / f"b_{uuid.uuid4().hex[:6]}.duckdb")
    con = duckdb_write(path)
    try:
        for s in sql:
            con.execute(s)
    finally:
        con.close()
    return path


def _diff(a, b, a_table, b_table, **kw):
    """Diff and close both sides, with a walk deep enough to bisect."""
    kw.setdefault("key", "id")
    kw.setdefault("threshold", 2)
    kw.setdefault("bisection_factor", 4)
    try:
        return diff(a, b, a_table, b_table, **kw)
    finally:
        a.close()
        b.close()


def _bq_bq(url, a_table, b_table, **kw):
    return _diff(open_bigquery(url, "A"), open_bigquery(url, "B"), a_table, b_table, **kw)


def _bq_duck(url, path, a_table, b_table, **kw):
    return _diff(open_bigquery(url, "A"), open_duckdb(path, "B"), a_table, b_table, **kw)


def _found(result):
    """(key, kind, sorted changed columns) per reported difference."""
    return [(d.key, d.kind, sorted(d.columns) if d.columns else d.columns) for d in result.diffs]


def _render(raw_type: str, literal: str, scale: int = 6) -> str:
    """The SQL rendering one literal of `raw_type` (bills nothing)."""
    d = BigQueryDialect(float_scale=scale)
    return f"select {d.normalize(_column('c', raw_type))} from (select {literal} as c)"


# ===================================================== findings (strict xfail)


@pytest.fixture(scope="module")
def nested(bq):
    """Two tables of ARRAY / STRUCT / GEOGRAPHY columns whose rows differ in
    stored value on every id but 1 - each pair renders alike through '%t'."""
    run, name, ds, _ = bq
    a, b = name("nested_a"), name("nested_b")
    common = "struct(1 as id, ['k'] as arr, struct('k' as x, 'k' as y) as st, st_geogpoint(5, 5) as g)"
    rows_a = [
        common,
        "struct(2, ['a, b'], struct('a, b', 'c'), st_geogpoint(1, 2))",
        "struct(3, [''], struct(cast(null as string), 'z'), st_geogpoint(3, 3))",
    ]
    rows_b = [
        common,
        "struct(2, ['a', 'b'], struct('a', 'b, c'), st_geogpoint(1.0000000000000002, 2))",
        "struct(3, cast([] as array<string>), struct('NULL', 'z'), st_geogpoint(3, 3))",
    ]
    for t, rows in ((a, rows_a), (b, rows_b)):
        run(f"create or replace table {ds}.`{t}` as select * from unnest([{', '.join(rows)}])")
    return a, b


@pytest.mark.xfail(
    strict=True,
    reason="RT-HB1: ARRAY renders through FORMAT('%t') with strings unquoted, so "
    "['a, b'] equals ['a', 'b'] and [''] equals [] - a false match",
)
def test_array_elements_keep_their_boundaries(bigquery_url, nested):
    result = _bq_bq(bigquery_url, *nested, columns=["arr"])
    assert _found(result) == [(2, "different", ["arr"]), (3, "different", ["arr"])]


def test_array_unquoted_strings_collide_in_the_rendering_itself(bq):
    """The mechanism behind RT-HB1, pinned on literals: if BigQuery ever
    quotes strings in '%t', this fails and RT-HB1 must be re-examined."""
    run, *_ = bq
    got = run(
        "select "
        + ", ".join(
            "(" + _render(t, v) + ")"
            for t, v in [
                ("ARRAY<STRING>", "['a, b']"),
                ("ARRAY<STRING>", "['a', 'b']"),
                ("ARRAY<BYTES>", "[b'a, b']"),
                ("ARRAY<BYTES>", "[b'a', b'b']"),
            ]
        )
    )[0]
    assert got[0] == got[1] == "[a, b]"
    assert got[2] == got[3] == "[a, b]"


@pytest.mark.xfail(
    strict=True,
    reason="RT-HB2: STRUCT renders through FORMAT('%t'): ('a, b', 'c') equals "
    "('a', 'b, c'), and a NULL field equals the string 'NULL' - a false match",
)
def test_struct_fields_keep_their_boundaries_and_nulls(bigquery_url, nested):
    result = _bq_bq(bigquery_url, *nested, columns=["st"])
    assert _found(result) == [(2, "different", ["st"]), (3, "different", ["st"])]


@pytest.mark.xfail(
    strict=True,
    reason="RT-HB3: GEOGRAPHY renders as WKT with 15 significant digits, so "
    "points stored with different coordinates (ST_X 0.9999999999999998 vs 1.0) "
    "compare identical - a false match",
)
def test_geography_points_that_are_stored_differently_differ(bigquery_url, nested, bq):
    run, _, ds, _ = bq
    a, b = nested
    # The negative control: the stored points really differ.
    xa, xb = (
        run(f"select to_hex(st_asbinary(g)) from {ds}.`{t}` where id = 2")[0][0]
        for t in (a, b)
    )
    assert xa != xb
    result = _bq_bq(bigquery_url, a, b, columns=["g"])
    assert _found(result) == [(2, "different", ["g"])]


@pytest.mark.xfail(
    strict=True,
    reason="RT-HB4: the hidden pseudo-column _PARTITIONTIME of an ingestion-time "
    "partitioned table is listed by INFORMATION_SCHEMA.COLUMNS and compared as "
    "data - a schema difference against any copy, every row against a reload",
)
def test_ingestion_time_partition_pseudo_column_is_not_data(bigquery_url, bq, tmp_path):
    run, name, ds, _ = bq
    t = name("ingest")
    run(f"create or replace table {ds}.`{t}` (id INT64, v STRING) partition by _PARTITIONDATE")
    run(f"insert into {ds}.`{t}` (id, v) values (1, 'a'), (2, 'b'), (3, 'c')")
    path = _duck(tmp_path, "create table t as select * from (values (1, 'a'), (2, 'B'), (3, 'c')) v(id, v)")
    result = _bq_duck(bigquery_url, path, t, "t")
    # Exactly the planted change; no `_PARTITIONTIME` schema difference.
    assert result.schema_differences == []
    assert _found(result) == [(2, "different", ["v"])]


@pytest.mark.xfail(
    strict=True,
    reason="RT-HB5: a table CLONE honours FOR SYSTEM_TIME AS OF but is read live "
    "(table_type 'CLONE' is not 'BASE TABLE'), with a caveat calling it a view",
)
def test_a_table_clone_is_read_at_one_snapshot(bigquery_url, bq):
    run, name, ds, _ = bq
    base, clone = name("cl_base"), name("cl_clone")
    run(f"create or replace table {ds}.`{base}` as select i as id from unnest(generate_array(1, 10)) i")
    run(f"create table {ds}.`{clone}` clone {ds}.`{base}`")
    a = open_bigquery(bigquery_url)
    try:
        key = a.key_spec(clone, "id")
        assert a.key_stats(clone, key).rows == 10
        run(f"insert into {ds}.`{clone}` (id) values (11)")  # the mid-walk write
        assert a.key_stats(clone, key).rows == 10
        assert a.caveats(list(key.columns)) == []
    finally:
        a.close()


#: Accepted by the row-filter check, yet BigQuery parses it as `(']' in [']'])
#: or (true ...)`: the `]` inside the string literal ends the check's `[...]`
#: region early, so the check counts the string's closing quote as an opening
#: one and misreads which parentheses are code.
BRACKET_ESCAPE = "']' in unnest([']'])) or (true or 'x' in unnest(['['])"


@pytest.mark.xfail(
    strict=True,
    reason="RT-HB6: the --where check skips `[` to the first `]` even inside a "
    "string literal, so a predicate with `[']']` escapes its parentheses (and "
    "the bucket bounds) on BigQuery",
)
def test_a_bracketed_string_cannot_escape_the_filter_parentheses():
    assert row_filter_problem(BRACKET_ESCAPE) is not None


def test_the_bracket_predicate_really_escapes_on_bigquery(bq):
    """The live half of RT-HB6: ANDed with a bound that excludes every row,
    the accepted predicate still returns all ten."""
    run, *_ = bq
    got = run(
        "select count(*) from unnest(generate_array(1, 10)) as id "
        f"where ({BRACKET_ESCAPE}) and id < 0"
    )
    assert got == [(10,)]


@pytest.mark.xfail(
    strict=True,
    reason="RT-HB7: a domain-scoped project (example.com:proj) cannot be named in "
    "a three-part table name; its dot splits it into four parts and it is refused",
)
def test_a_domain_scoped_project_can_be_named_in_the_table():
    d = BigQueryDialect(side="A")
    d.project = "billing-project"
    assert d._resolve("example.com:proj.ds.t") == ("example.com:proj", "ds", "t")


# ======================================================= regression (no bug)


def test_float_sample_renders_exactly_the_spec_at_three_scales(bq):
    """The 1,565-value adversarial float sample, rendered by BigQuery from
    literals (nothing billed), equals `canonical_float_text` at scales 0, 6, 20.
    Non-finite and out-of-range values are excluded (the spec refuses them)."""
    from float_sample import adversarial_doubles

    run, *_ = bq
    values = [
        x for x in adversarial_doubles() if x == x and abs(x) < 2.0**116 and x not in (float("inf"), -float("inf"))
    ]
    for scale in (0, 6, 20):
        d = BigQueryDialect(float_scale=scale)
        expr = d.normalize(_column("c", "FLOAT64"))
        bad = []
        for i in range(0, len(values), 400):
            chunk = values[i : i + 400]
            lits = ", ".join(f"struct({j} as i, cast('{x!r}' as float64) as c)" for j, x in enumerate(chunk))
            rows = run(f"select i, {expr} from unnest([{lits}]) order by i")
            bad += [
                (chunk[j], got, canonical_float_text(chunk[j], scale))
                for j, got in rows
                if got != canonical_float_text(chunk[j], scale)
            ]
        assert not bad, bad[:5]


def test_text_keys_differing_only_by_case_are_distinct_under_und_ci(bigquery_url, bq, tmp_path):
    """A STRING COLLATE 'und:ci' key: 'a', 'A' and 'a ' are three keys (the
    uniqueness count is not under the collation), and a change on 'A' alone is
    found under its real text. The value column is case-insensitive too, and
    'ABC' against 'abc' is a difference."""
    run, name, ds, _ = bq
    t = name("ci_key")
    run(
        f"create or replace table {ds}.`{t}` (k STRING COLLATE 'und:ci', v STRING COLLATE 'und:ci')"
    )
    run(f"insert into {ds}.`{t}` values ('a', 'x'), ('A', 'y'), ('a ', 'z'), ('b', 'ABC')")
    path = _duck(
        tmp_path,
        "create table t as select * from (values ('a', 'x'), ('A', 'Y'), ('a ', 'z'), ('b', 'abc')) v(k, v)",
    )
    result = _bq_duck(bigquery_url, path, t, "t", key="k")
    assert sorted(_found(result)) == [("A", "different", ["v"]), ("b", "different", ["v"])]


def test_mixed_type_composite_key_matches_postgres_exactly(bigquery_url, bq, pgw):
    """A five-part key - STRING, DATE, NUMERIC, BYTES, TIMESTAMP - against
    PostgreSQL; a NUMERIC key 1.50 equals numeric 1.5, and the one planted
    change (a microsecond in a value timestamp) is found under the right key."""
    run, name, ds, _ = bq
    t = name("ckey")
    run(
        f"create or replace table {ds}.`{t}` as select * from unnest(["
        "struct('x' as s, date '0001-01-01' as d, numeric '1.50' as n, b'\\x00' as b, "
        "timestamp '9999-12-31 23:59:59.999999+00' as ts, datetime '2024-01-01 00:00:00.000001' as v), "
        "struct('x', date '9999-12-31', numeric '-0.000000001', b'', timestamp '0001-01-01 00:00:00+00', "
        "datetime '2024-01-01 00:00:00')])"
    )
    p = pgw.table("ckey", "s text, d date, n numeric, b bytea, ts timestamptz, v timestamp")
    pgw.execute(
        f"insert into {p} values ('x', '0001-01-01', 1.5, '\\x00', '9999-12-31 23:59:59.999999+00', "
        "'2024-01-01 00:00:00.000002'), ('x', '9999-12-31', -0.000000001, '', "
        "'0001-01-01 00:00:00+00', '2024-01-01 00:00:00')"
    )
    result = _diff(
        open_bigquery(bigquery_url, "A"), open_pg(PG_URL, "B"), t, f"public.{p}",
        key=["s", "d", "n", "b", "ts"],
    )
    assert len(result.diffs) == 1
    assert result.diffs[0].kind == "different" and result.diffs[0].columns == ["v"]
    assert "0001-01-01" in str(result.diffs[0].key) and "1.5" in str(result.diffs[0].key)


def test_bignumeric_extremes_match_postgres_and_a_last_digit_differs(bigquery_url, bq, pgw):
    """BIGNUMERIC at its maximum (39 integer digits, 38 places) against
    PostgreSQL numeric: a key that wide renders exactly, a value at the float
    scale agrees, and a change in the 6th place is found."""
    run, name, ds, _ = bq
    big = "578960446186580977117854925043439539266.34992332820282019728792003956564819967"
    t = name("bignum")
    run(
        f"create or replace table {ds}.`{t}` as select * from unnest(["
        f"struct(bignumeric '{big}' as id, bignumeric '{big}' as v), "
        f"struct(bignumeric '-{big}', bignumeric '0.000000499999'), "
        f"struct(bignumeric '1e-38', bignumeric '12345678901234567890123456789012345678.123456')])"
    )
    p = pgw.table("bignum", "id numeric, v numeric")
    pgw.execute(
        f"insert into {p} values ({big}, {big}), (-{big}, 0), "
        "(1e-38, 12345678901234567890123456789012345678.123457)"
    )
    result = _diff(open_bigquery(bigquery_url, "A"), open_pg(PG_URL, "B"), t, f"public.{p}")
    assert _found(result) == [("0.00000000000000000000000000000000000001", "different", ["v"])]


def test_parameterised_numeric_and_string_types_compare_by_value(bigquery_url, bq, tmp_path):
    """NUMERIC(10, 2), NUMERIC(5), BIGNUMERIC(50, 20), STRING(10), BYTES(4)
    against DuckDB's plain types: equal values match, one planted change in
    each column is found."""
    run, name, ds, _ = bq
    t = name("param")
    run(
        f"create or replace table {ds}.`{t}` (id INT64, n NUMERIC(10, 2), n0 NUMERIC(5), "
        "bn BIGNUMERIC(50, 20), s STRING(10), b BYTES(4))"
    )
    run(
        f"insert into {ds}.`{t}` values (1, 12345678.91, 99999, 1.12345678901234567890, 'abcdefghij', b'\\x00\\x01\\x02\\x03'), "
        "(2, -0.01, -99999, -0.0000005, '', b''), (3, 0, 0, 0, 'z', b'z')"
    )
    path = _duck(
        tmp_path,
        "create table t (id bigint, n decimal(10,2), n0 decimal(5,0), bn decimal(38,20), s varchar, b blob)",
        "insert into t values (1, 12345678.91, 99999, 1.12345678901234567890, 'abcdefghij', '\\x00\\x01\\x02\\x03'::blob), "
        "(2, -0.01, -99999, -0.0000005, '', ''::blob), (3, 0.01, 1, 0, 'Z', 'y'::blob)",
    )
    result = _bq_duck(bigquery_url, path, t, "t")
    assert _found(result) == [(3, "different", ["b", "n", "n0", "s"])]


def test_datetime_and_timestamp_extremes_against_duckdb(bigquery_url, bq, tmp_path):
    """DATETIME is naive and TIMESTAMP an instant: each matches DuckDB's type
    of the same meaning at 0001-01-01 and 9999-12-31 23:59:59.999999, TIME at
    its ends, and a one-microsecond change in each is found."""
    run, name, ds, _ = bq
    t = name("times")
    run(
        f"create or replace table {ds}.`{t}` as select * from unnest(["
        "struct(1 as id, datetime '0001-01-01 00:00:00' as dt, timestamp '9999-12-31 23:59:59.999999+00' as ts, "
        "time '23:59:59.999999' as tm, date '0001-01-01' as d), "
        "struct(2, datetime '9999-12-31 23:59:59.999999', timestamp '0001-01-01 00:00:00+00', time '00:00:00', date '9999-12-31'), "
        "struct(3, datetime '2024-02-29 13:04:05.123456', timestamp '2024-06-15 12:00:00.000001+00', time '12:00:00.000001', date '0999-12-25')])"
    )
    path = _duck(
        tmp_path,
        "set TimeZone='UTC'",
        "create table t (id bigint, dt timestamp, ts timestamptz, tm time, d date)",
        "insert into t values (1, '0001-01-01 00:00:00', '9999-12-31 23:59:59.999999+00', '23:59:59.999999', '0001-01-01'), "
        "(2, '9999-12-31 23:59:59.999999', '0001-01-01 00:00:00+00', '00:00:00', '9999-12-31'), "
        "(3, '2024-02-29 13:04:05.123457', '2024-06-15 12:00:00+00', '12:00:00', '0999-12-26')",
    )
    result = _bq_duck(bigquery_url, path, t, "t")
    assert _found(result) == [(3, "different", ["d", "dt", "tm", "ts"])]
    assert not [w for w in result.warnings if "timezone-aware" in w], result.warnings


def test_timestamp_against_a_naive_column_warns(bigquery_url, bq, tmp_path):
    """The naming trap: BigQuery TIMESTAMP is the instant. Against DuckDB's
    naive TIMESTAMP the run must warn, and still find the planted change."""
    run, name, ds, _ = bq
    t = name("tstrap")
    run(
        f"create or replace table {ds}.`{t}` as select * from unnest(["
        "struct(1 as id, timestamp '2024-01-01 10:00:00+00' as ts), struct(2, timestamp '2024-01-01 11:00:00+00')])"
    )
    path = _duck(
        tmp_path,
        "create table t as select * from (values (1, timestamp '2024-01-01 10:00:00'), (2, timestamp '2024-01-01 11:00:01')) v(id, ts)",
    )
    result = _bq_duck(bigquery_url, path, t, "t")
    assert _found(result) == [(2, "different", ["ts"])]
    assert [w for w in result.warnings if "timezone-aware on side A" in w]


def test_json_documents_against_postgres_jsonb(bigquery_url, bq, pgw):
    """JSON: key order and spacing are layout, so they match jsonb; a changed
    number and a JSON null against SQL NULL are differences."""
    run, name, ds, _ = bq
    t = name("json")
    run(
        f"create or replace table {ds}.`{t}` as select * from unnest(["
        "struct(1 as id, json '{\"b\": [1, 2], \"a\": \"x\"}' as j), "
        "struct(2, json '{\"n\": 3}'), struct(3, cast(null as json))])"
    )
    p = pgw.table("json", "id bigint, j jsonb")
    pgw.execute(f"insert into {p} values (1, '{{\"a\":\"x\",\"b\":[1,2]}}'), (2, '{{\"n\": 4}}'), (3, 'null')")
    result = _diff(open_bigquery(bigquery_url, "A"), open_pg(PG_URL, "B"), t, f"public.{p}")
    assert _found(result) == [(2, "different", ["j"]), (3, "different", ["j"])]


def test_require_partition_filter_fails_naming_the_side_and_where_satisfies_it(
    bigquery_url, bq, tmp_path
):
    """A table that requires a partition filter: without --where the run
    fails with an error naming side A and the filter column; with a --where
    on the partition column it runs and finds the planted change."""
    run, name, ds, _ = bq
    t = name("reqp")
    run(
        f"create or replace table {ds}.`{t}` (id INT64, d DATE, v STRING) partition by d "
        "cluster by v options(require_partition_filter=true)"
    )
    run(f"insert into {ds}.`{t}` values (1, '2024-01-01', 'a'), (2, '2024-01-02', 'b'), (3, '2024-01-03', 'c')")
    path = _duck(
        tmp_path,
        "create table t as select * from (values (1, date '2024-01-01', 'a'), (2, date '2024-01-02', 'B'), (3, date '2024-01-03', 'c')) v(id, d, v)",
    )
    with pytest.raises(ValueError) as exc:
        _bq_duck(bigquery_url, path, t, "t")
    assert "side A" in str(exc.value) and "partition" in str(exc.value)
    result = _bq_duck(bigquery_url, path, t, "t", where_a="d >= '2024-01-01'")
    assert _found(result) == [(2, "different", ["v"])]


def test_materialized_view_is_read_live_and_compared(bigquery_url, bq, tmp_path):
    """A materialized view refuses time travel ('do not support time travel'),
    so it must be read live, with the caveat, and find the planted change.
    (Table SNAPSHOTs are not built here: the test account may create them but
    lacks bigquery.tables.deleteSnapshot, so they could not be cleaned up.)"""
    run, name, ds, _ = bq
    base, mv = name("mvbase"), name("mv")
    run(f"create or replace table {ds}.`{base}` as select i as id, cast(i as string) as v from unnest(generate_array(1, 5)) i")
    run(f"create materialized view {ds}.`{mv}` as select id, v from {ds}.`{base}`")
    path = _duck(
        tmp_path,
        "create table t as select i::bigint as id, (case when i = 4 then 'four' else i::varchar end) as v from range(1, 6) r(i)",
    )
    result = _bq_duck(bigquery_url, path, mv, "t")
    assert _found(result) == [(4, "different", ["v"])]
    assert any("read live" in w for w in result.warnings)


@pytest.mark.xfail(
    strict=True,
    reason="RT-HB8: a table in another region (US public data from an EU "
    "connection) is reported as missing or inaccessible; nothing points at the "
    "location or at ?location=",
)
def test_a_table_in_another_location_fails_naming_the_location(bigquery_url):
    """The test dataset is in the EU; a US public table cannot be read by a job
    in the EU (with `?location=US` the same call succeeds). The error must
    name the side and the location, not just 'not found'. Bills nothing."""
    a = open_bigquery(bigquery_url, "A")
    try:
        with pytest.raises(ValueError) as exc:
            a.columns("bigquery-public-data.samples.shakespeare")
        message = str(exc.value)
        assert "side A" in message
        assert "location" in message.lower()
    finally:
        a.close()


@pytest.mark.parametrize(
    "statement",
    [
        "insert into {t} (id) values (99)",
        "delete from {t} where true",
        "update {t} set v = 'w' where true",
        "merge {t} as d using (select 1 as id) as s on d.id = s.id when matched then delete",
        "truncate table {t}",
        "create or replace table {t} as select 1 as id",
        "drop table {t}",
        "alter table {t} add column z INT64",
        "execute immediate 'delete from {t} where true'",
        "begin transaction; delete from {t} where true; commit transaction",
        "declare x int64 default 1; insert into {t} (id) values (98); select x",
        "create temp table tt as select 1 as a",
        "export data options(uri='gs://rth-bq-no-such-bucket-zz/x-*.csv', format='CSV') as select 1 as a",
    ],
)
def test_every_write_or_script_is_refused_by_the_server(bigquery_url, bq, statement):
    """The read-only scope is the server's refusal: DML, DDL, scripts, dynamic
    SQL, transactions. The planted row count must be untouched afterwards."""
    run, name, ds, _ = bq
    t = f"{ds}.`{_shared_rw_table(bq)}`"
    a = open_bigquery(bigquery_url)
    try:
        with pytest.raises(Exception):
            a.query(statement.format(t=t))
    finally:
        a.close()
    assert run(f"select count(*), countif(v = 'w') from {t}") == [(3, 0)]


def test_a_script_that_only_reads_runs_under_the_read_only_scope(bigquery_url, bq):
    """Pinned so the dialect's docstring can be corrected: the server does NOT
    refuse multi-statement scripts as such - one that only reads (DECLARE, a
    temp function, SELECT) runs - while every write inside one is refused
    (above). The tool never sends a script; `;` is refused in --where."""
    a = open_bigquery(bigquery_url)
    try:
        assert a.query("declare x int64 default 41; select x + 1") == [(42,)]
        assert a.query("create temp function f() as (7); select f()") == [(7,)]
    finally:
        a.close()
    assert row_filter_problem("1=1; select 1") is not None


def test_a_table_in_another_location_reads_with_the_location_parameter(bigquery_url):
    """The control for RT-HB8: `?location=US` reaches the US table (metadata
    only, nothing billed)."""
    a = open_bigquery(bigquery_url + "?location=US", "A")
    try:
        cols = [c.name for c in a.columns("bigquery-public-data.samples.shakespeare")]
        assert cols == ["word", "word_count", "corpus", "corpus_date"]
    finally:
        a.close()


_RW_TABLE: list[str] = []


def _shared_rw_table(bq) -> str:
    """One three-row table every write probe aims at, built once."""
    if not _RW_TABLE:
        run, name, ds, _ = bq
        t = name("rwprobe")
        run(f"create or replace table {ds}.`{t}` as select i as id, 'v' as v from unnest([1, 2, 3]) i")
        _RW_TABLE.append(t)
    return _RW_TABLE[0]


@pytest.mark.parametrize(
    "predicate, refused",
    [
        ("r'a(b' = 'a(b' and id > 1", False),
        ("b'x' = b'x' and id > 1", False),
        ('"it\'s" = "it\'s" and id > 1', False),
        ("`id` > 1", False),
        ("id > 1 # comment", True),
        ("id > 1 -- comment", True),
        ("'''a''' = 'a'", True),
        ("'a\\'' = 'a'", True),
        ("id > 1; delete from t where true", True),
        ("id > 1) or (true", True),
        ("r\"\\\" = r\"\\\"", True),
    ],
)
def test_where_with_bigquery_quoting(bigquery_url, bq, tmp_path, predicate, refused):
    """BigQuery's own literal forms work in --where (raw, bytes, double-quoted
    strings, backtick identifiers) and the plant inside the filter is found
    while the one outside is not; comment markers, triple quotes, backslash
    quotes, `;` and a parenthesis escape are refused before any query."""
    run, name, ds, _ = bq
    t = _where_table(bq)
    path = _duck(
        tmp_path,
        "create table t as select * from (values (1, 'X'), (2, 'b'), (3, 'C')) v(id, v)",
    )
    if refused:
        with pytest.raises(ValueError, match="row filter"):
            _bq_duck(bigquery_url, path, t, "t", where_a=predicate, where_b="id > 1")
        return
    result = _bq_duck(bigquery_url, path, t, "t", where_a=predicate, where_b="id > 1")
    assert _found(result) == [(3, "different", ["v"])]


_WHERE_TABLE: list[str] = []


def _where_table(bq) -> str:
    if not _WHERE_TABLE:
        run, name, ds, _ = bq
        t = name("where")
        run(f"create or replace table {ds}.`{t}` as select * from unnest([struct(1 as id, 'a' as v), struct(2, 'b'), struct(3, 'c')])")
        _WHERE_TABLE.append(t)
    return _WHERE_TABLE[0]


def test_odd_identifiers_columns_and_map(bigquery_url, bq, tmp_path):
    """A flexible column name with a space and a dash, a reserved word, and a
    mixed-case name matched case-insensitively; `--map` pairs a renamed
    column. The one planted change is found under side A's spelling."""
    run, name, ds, _ = bq
    t = name("odd ident-ü")
    run(
        f"create or replace table {ds}.`{t}` (`ID` INT64, `my col-1` STRING, `select` INT64, `MixedCase` STRING, old_name STRING)"
    )
    run(f"insert into {ds}.`{t}` values (1, 'a', 1, 'm', 'o'), (2, 'b', 2, 'n', 'p')")
    path = _duck(
        tmp_path,
        'create table t ("id" bigint, "my col-1" varchar, "select" bigint, mixedcase varchar, new_name varchar)',
        "insert into t values (1, 'a', 1, 'm', 'o'), (2, 'b', 2, 'n', 'q')",
    )
    result = _bq_duck(bigquery_url, path, t, "t", key="id", column_map={"old_name": "new_name"})
    assert _found(result) == [(2, "different", ["old_name"])]


def test_cli_end_to_end_json_exit_code_and_no_credentials(bigquery_url, bq, tmp_path):
    """The CLI against a BigQuery side: exit 1 on the planted difference, a
    JSON verdict naming it, and nothing credential-like in the output."""
    run, name, ds, _ = bq
    t = _where_table(bq)
    path = _duck(tmp_path, "create table t as select * from (values (1, 'a'), (2, 'b'), (3, 'Z')) v(id, v)")
    env = dict(os.environ)
    proc = subprocess.run(
        [sys.executable, "-m", "parity.cli", "diff", "--a", bigquery_url, "--a-table", t,
         "--b", f"duckdb:///{path}", "--b-table", "t", "--key", "id", "--json"],
        capture_output=True, text=True, env=env, timeout=600,
    )
    assert proc.returncode == 1, proc.stderr[-2000:]
    out = json.loads(proc.stdout)
    assert [(d["key"], d["kind"]) for d in out["differences"]] == [(3, "different")]
    blob = proc.stdout + proc.stderr
    for secret in ("access_token", "refresh_token", "private_key", "Bearer "):
        assert secret not in blob


@pytest.mark.xfail(
    strict=True,
    reason="RT-HB9: each FLOAT64 column's rendering is ~2,200 characters, so a "
    "table of 500 FLOAT64 columns exceeds BigQuery's 1,024K-character query "
    "limit and cannot be compared at all",
)
def test_a_table_of_500_float_columns_can_be_compared(bigquery_url, bq, tmp_path):
    run, name, ds, _ = bq
    n = 500
    t = name("widef")
    cols = ", ".join(f"cast(i * {k} as float64) / 7 as f{k}" for k in range(n))
    run(f"create or replace table {ds}.`{t}` as select i as id, {cols} from unnest(generate_array(1, 3)) i")
    dcols = ", ".join(f"(i * {k})::double / 7 as f{k}" for k in range(n))
    path = _duck(
        tmp_path,
        f"create table t as select i::bigint as id, {dcols} from range(1, 4) r(i)",
        "update t set f499 = 0 where id = 2",
    )
    result = _bq_duck(bigquery_url, path, t, "t", threshold=10_000)
    assert _found(result) == [(2, "different", ["f499"])]


def test_the_rendering_length_that_breaks_the_query_limit():
    """The arithmetic behind RT-HB9, offline: the FLOAT64 expression alone is
    over 2,000 characters, so ~460 columns reach 1,024K."""
    d = BigQueryDialect()
    assert len(d.normalize(_column("f499", "FLOAT64"))) > 2000


@pytest.mark.parametrize(
    "ddl",
    [
        "alter table {t} drop column v",
        "alter table {t} rename column v to w",
        "create or replace table {t} as select i as id, 'new' as v from unnest(generate_array(1, 12)) i",
        "update {t} set v = 'changed' where id = 3",
    ],
)
def test_schema_changes_and_replacement_mid_walk_stay_invisible(bigquery_url, bq, ddl):
    """Time travel covers the table's schema and identity too: after a column
    is dropped or renamed, or the whole table replaced, mid-walk, the pinned
    dialect still reads the version it started on (checksums unchanged); a
    fresh dialect sees the change (the control)."""
    run, name, ds, _ = bq
    t = name(f"sch{abs(hash(ddl)) % 10_000}")
    run(f"create or replace table {ds}.`{t}` as select i as id, cast(i as string) as v from unnest(generate_array(1, 10)) i")
    a = open_bigquery(bigquery_url)
    try:
        cols = a.columns(t)
        key = a.key_spec(t, "id")
        rest = [c for c in cols if c.name != "id"]
        before = a.segment_checksums(t, key, rest, 1, 11, 1)
        run(ddl.format(t=f"{ds}.`{t}`"))
        assert a.segment_checksums(t, key, rest, 1, 11, 1) == before
        assert a.key_stats(t, key).rows == 10
    finally:
        a.close()
    fresh = open_bigquery(bigquery_url)
    try:
        cols = fresh.columns(t)
        key = fresh.key_spec(t, "id")
        rest = [c for c in cols if c.name != "id"]
        assert [c.name for c in rest] != ["v"] or fresh.segment_checksums(
            t, key, rest, 1, 13, 1
        ) != before
    finally:
        fresh.close()


@pytest.mark.xfail(
    strict=True,
    reason="RT-HB10: a table written as the BigQuery console copies it, "
    "`project.dataset.table` in backticks, is refused as 'Invalid project ID' "
    "or 'table not found' with no word about the backticks",
)
@pytest.mark.parametrize("form", ["`{t}`", "`{p}.{d}.{t}`", "`{p}`.`{d}`.`{t}`"])
def test_a_backtick_quoted_table_name_is_read_or_explained(bigquery_url, bq, form):
    t = _where_table(bq)
    project, dataset, _ = parse_bigquery_url(bigquery_url)
    a = open_bigquery(bigquery_url, "A")
    try:
        try:
            cols = a.columns(form.format(p=project, d=dataset, t=t))
        except ValueError as exc:
            assert "backtick" in str(exc)
        else:
            assert [c.name for c in cols] == ["id", "v"]
    finally:
        a.close()


def test_a_wildcard_table_name_is_refused_naming_the_side(bigquery_url, bq):
    """`rth_bq_where_*` is BigQuery wildcard syntax; parity reads one table
    and must say it found none (naming the side), not query a union."""
    _where_table(bq)
    a = open_bigquery(bigquery_url, "A")
    try:
        with pytest.raises(ValueError) as exc:
            a.columns("rth_bq_where_*")
        assert "side A" in str(exc.value) and "not found" in str(exc.value)
    finally:
        a.close()


def test_unicode_and_control_text_against_duckdb(bigquery_url, bq, tmp_path):
    """NUL, chr(31), backslash-N, every plane (BMP, astral emoji, U+10FFFF),
    RTL, combining marks against precomposed, whitespace-only: equal text
    matches DuckDB, and NFC against NFD is a difference (bytes differ)."""
    run, name, ds, _ = bq
    vals = [
        "a\x00b", "a\x1fb", "\\N", "\U0001F600", "\U0010FFFF", "שלום",
        "é", " \t ", "",
    ]
    t = name("uni")
    lits = ", ".join(
        f"struct({i} as id, code_points_to_string([{', '.join(str(ord(ch)) for ch in v)}]) as s)"
        if v else f"struct({i} as id, '' as s)"
        for i, v in enumerate(vals, 1)
    )
    run(f"create or replace table {ds}.`{t}` as select * from unnest([{lits}])")
    path = str(tmp_path / "u.duckdb")
    con = duckdb_write(path)
    try:
        con.execute("create table t (id bigint, s varchar)")
        b_vals = list(vals)
        b_vals[6] = "é"  # NFC on side B, NFD on side A: a real difference
        con.executemany("insert into t values (?, ?)", list(enumerate(b_vals, 1)))
    finally:
        con.close()
    result = _bq_duck(bigquery_url, path, t, "t")
    assert _found(result) == [(7, "different", ["s"])]


def test_200k_rows_with_first_last_and_middle_plants(bigquery_url, bq, tmp_path):
    """Scale within the cost budget: 200,000 rows, differences on the first
    key, the last key and one in the middle, nothing else."""
    run, name, ds, _ = bq
    t = name("scale")
    run(
        f"create or replace table {ds}.`{t}` as select i as id, "
        "concat('v', cast(i as string)) as v, cast(i as numeric) / 8 as n "
        "from unnest(generate_array(1, 200000)) i"
    )
    path = _duck(
        tmp_path,
        "create table t as select i::bigint as id, 'v' || i::varchar as v, (i::decimal(18,3) / 8)::decimal(18,3) as n from range(1, 200001) r(i)",
        "update t set v = 'x' where id in (1, 200000)",
        "update t set n = n + 0.001 where id = 123457",
    )
    result = _bq_duck(bigquery_url, path, t, "t", threshold=1000, bisection_factor=16)
    assert _found(result) == [
        (1, "different", ["v"]),
        (123457, "different", ["n"]),
        (200000, "different", ["v"]),
    ]
    assert result.stats.rows_downloaded < 10_000
