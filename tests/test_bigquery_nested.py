"""BigQuery ARRAY, STRUCT and GEOGRAPHY: injective renderings (RT-HB1..HB3).

'%t' printed strings unquoted, so ['a, b'] equalled ['a', 'b'], [''] equalled
[], ('a, b', 'c') equalled ('a', 'b, c') and a NULL field equalled 'NULL'; WKT
kept 15 significant digits. ARRAY now follows the shared LIST rule
(`canonical_list_text`) - so it matches DuckDB and PostgreSQL lists and the
sample check reads it - STRUCT follows `canonical_struct_text`, GEOGRAPHY is
its WKB. Offline tests pin the parsing; the live ones (marker `bigquery`)
run on literals, which bill nothing, and on two tiny tables prefixed
`rthfix_bq_`, dropped at teardown.
"""

from __future__ import annotations

import uuid

import pytest

from parity.dialects.base import (
    LIST_TERMINATOR,
    STRUCT_FIELD_MARK,
    canonical_list_text,
    canonical_struct_text,
)
from parity.dialects.bigquery_dialect import (
    BigQueryDialect,
    _column,
    _split_table_name,
    _struct_fields,
    map_type_bigquery,
    parse_bigquery_url,
)
from parity.types import LogicalType

RUN = uuid.uuid4().hex[:8]


# ------------------------------------------------------------------ offline


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("ARRAY<STRING>", LogicalType.LIST),
        ("ARRAY<STRING(10)>", LogicalType.LIST),
        ("ARRAY<NUMERIC(10, 2)>", LogicalType.LIST),
        ("ARRAY<FLOAT64>", LogicalType.LIST),
        ("ARRAY<TIMESTAMP>", LogicalType.LIST),
        ("ARRAY<BYTES>", LogicalType.LIST),
        ("ARRAY<JSON>", LogicalType.UNKNOWN),
        ("ARRAY<GEOGRAPHY>", LogicalType.UNKNOWN),
        ("ARRAY<STRUCT<a INT64>>", LogicalType.UNKNOWN),
        ("STRUCT<a INT64, b STRING>", LogicalType.UNKNOWN),
        ("GEOGRAPHY", LogicalType.UNKNOWN),
        ("INTERVAL", LogicalType.UNKNOWN),
        ("NUMERIC(10, 2)", LogicalType.DECIMAL),
    ],
)
def test_type_map(raw, kind):
    assert map_type_bigquery(raw) is kind


def test_struct_fields_are_parsed_with_nesting_parameters_and_backticks():
    assert _struct_fields(
        "a INT64, b NUMERIC(10, 2), c ARRAY<STRUCT<x STRING, y INT64>>"
    ) == [
        ("a", "INT64"),
        ("b", "NUMERIC(10, 2)"),
        ("c", "ARRAY<STRUCT<x STRING, y INT64>>"),
    ]
    assert _struct_fields("`a b` STRING, `c\\`d` INT64") == [
        ("a b", "STRING"),
        ("c`d", "INT64"),
    ]
    # Anonymous fields (a view's) cannot be read by name.
    assert _struct_fields("INT64, STRING") is None
    assert _struct_fields("STRUCT<a INT64>") is None


def test_the_struct_spec_keeps_boundaries_nulls_and_names_apart():
    texts = {
        canonical_struct_text([("x", "a, b"), ("y", "c")]),
        canonical_struct_text([("x", "a"), ("y", "b, c")]),
        canonical_struct_text([("x", "\\N"), ("y", "z")]),
        canonical_struct_text([("x", "NULL"), ("y", "z")]),
        canonical_struct_text([("w", "NULL"), ("y", "z")]),
    }
    assert len(texts) == 5
    assert (
        canonical_struct_text([("x", "1")])
        == "{x" + STRUCT_FIELD_MARK + "1" + LIST_TERMINATOR + "}"
    )
    # A name holding the mark is escaped, so it cannot forge a boundary.
    assert canonical_struct_text([("a\\=b", "1")]) != canonical_struct_text(
        [("a", "b\\=1")]
    )


def test_the_dialect_spells_the_struct_mark_from_its_code_point():
    sql = BigQueryDialect().normalize(_column("s", "STRUCT<x STRING>"))
    assert "concat(chr(92), '=')" in sql and "\\=" not in sql
    assert "from unnest([`s`])" in sql


def test_arrays_are_read_in_offset_order_and_null_safe():
    sql = BigQueryDialect().normalize(_column("a", "ARRAY<INT64>"))
    assert "with offset as `__parity_offset`" in sql
    assert "order by `__parity_offset`" in sql
    assert sql.startswith("coalesce(if(`a` is null, null, concat('[', ifnull(")


def test_geography_renders_its_wkb():
    sql = BigQueryDialect().normalize(_column("g", "GEOGRAPHY"))
    assert sql == "coalesce(to_hex(st_asbinary(`g`)), concat(chr(92), 'N'))"


def test_a_list_key_is_refused():
    from parity.types import KeySpec

    d = BigQueryDialect(side="A")
    col = _column("k", "ARRAY<INT64>")
    with pytest.raises(ValueError, match="cannot be used as a key"):
        d.check_key("t", KeySpec((col,), hashed=True))


@pytest.mark.parametrize(
    ("name", "parts"),
    [
        ("p.d.t", ["p", "d", "t"]),
        ("`p.d.t`", ["p", "d", "t"]),
        ("`p`.`d`.`t`", ["p", "d", "t"]),
        ("`t`", ["t"]),
        ("example.com:proj.ds.t", ["example.com:proj", "ds", "t"]),
        ("`example.com:proj`.ds.t", ["example.com:proj", "ds", "t"]),
        ("`example.com:proj.ds.t`", ["example.com:proj", "ds", "t"]),
        ("a`b.c", None),
        ("`a.b`c", None),
        ("`a", None),
    ],
)
def test_table_names_with_backticks_and_domains(name, parts):
    assert _split_table_name(name) == parts


def test_a_misplaced_backtick_is_refused_naming_backticks():
    d = BigQueryDialect(side="A")
    d.project = "p"
    with pytest.raises(ValueError, match="backticks"):
        d._resolve("d.t`x`")


# --------------------------------------------------------------------- live


@pytest.fixture(scope="module")
def client(bigquery_url):
    from google.cloud import bigquery

    project, dataset, location = parse_bigquery_url(bigquery_url)
    c = bigquery.Client(project=project, location=location)
    made: list[str] = []
    yield c, project, dataset, made
    for t in made:
        c.delete_table(f"{project}.{dataset}.{t}", not_found_ok=True)
    c.close()


#: (BigQuery type, literal, expected canonical text).
LITERALS = [
    ("ARRAY<STRING>", "['a, b']", canonical_list_text(["a, b"])),
    ("ARRAY<STRING>", "['a', 'b']", canonical_list_text(["a", "b"])),
    ("ARRAY<STRING>", "['']", canonical_list_text([""])),
    ("ARRAY<STRING>", "cast([] as array<string>)", "[]"),
    ("ARRAY<STRING>", "cast(null as array<string>)", "\\N"),
    (
        "ARRAY<STRING>",
        "['\\\\N', concat('a', chr(31), 'b')]",
        canonical_list_text(["\\\\N", "a\\sb"]),
    ),
    ("ARRAY<INT64>", "[3, 1, 2]", canonical_list_text(["3", "1", "2"])),
    (
        "ARRAY<FLOAT64>",
        "[1.5, 1e15+0.5]",
        canonical_list_text(["1.500000", "1000000000000000.500000"]),
    ),
    ("ARRAY<BYTES>", "[b'a, b']", canonical_list_text(["\\\\x612c2062"])),
    (
        "ARRAY<TIMESTAMP>",
        "[timestamp '2024-01-01 12:00:00+02']",
        canonical_list_text(["2024-01-01 10:00:00.000000"]),
    ),
    (
        "STRUCT<x STRING, y STRING>",
        "struct('a, b' as x, 'c' as y)",
        canonical_struct_text([("x", "a, b"), ("y", "c")]),
    ),
    (
        "STRUCT<x STRING, y STRING>",
        "struct('a' as x, 'b, c' as y)",
        canonical_struct_text([("x", "a"), ("y", "b, c")]),
    ),
    (
        "STRUCT<x STRING, y STRING>",
        "struct(cast(null as string) as x, 'z' as y)",
        canonical_struct_text([("x", "\\N"), ("y", "z")]),
    ),
    (
        "STRUCT<x STRING, y STRING>",
        "struct('NULL' as x, 'z' as y)",
        canonical_struct_text([("x", "NULL"), ("y", "z")]),
    ),
    ("STRUCT<x STRING>", "cast(null as struct<x string>)", "\\N"),
    (
        "STRUCT<a ARRAY<INT64>, b STRUCT<c FLOAT64, d BOOL>>",
        "struct([1, 2] as a, struct(0.1 as c, cast(null as bool) as d) as b)",
        canonical_struct_text(
            [
                ("a", canonical_list_text(["1", "2"])),
                ("b", canonical_struct_text([("c", "0.100000"), ("d", "\\N")])),
            ]
        ),
    ),
    (
        "ARRAY<STRUCT<x STRING, n INT64>>",
        "[struct('p' as x, 1 as n), struct(cast(null as string), 2)]",
        canonical_list_text(
            [
                canonical_struct_text([("x", "p"), ("n", "1")]),
                canonical_struct_text([("x", "\\N"), ("n", "2")]),
            ]
        ),
    ),
    (
        "STRUCT<`a b` STRING>",
        "struct('v' as `a b`)",
        canonical_struct_text([("a b", "v")]),
    ),
]


@pytest.mark.bigquery
def test_each_literal_renders_exactly_its_spec(client):
    """Bills nothing: no table is read."""
    c, *_ = client
    d = BigQueryDialect()
    sql = " union all ".join(
        f"select {i} as i, {d.normalize(_column('c', t))} as r from (select {lit} as c)"
        for i, (t, lit, _) in enumerate(LITERALS)
    )
    got = {r[0]: r[1] for r in c.query_and_wait(sql)}
    bad = [
        (t, lit, got[i], want)
        for i, (t, lit, want) in enumerate(LITERALS)
        if got[i] != want
    ]
    assert not bad, bad


@pytest.mark.bigquery
def test_geography_points_stored_differently_render_differently(client):
    c, *_ = client
    sql = BigQueryDialect().normalize(_column("g", "GEOGRAPHY"))
    got = [
        r[0]
        for r in c.query_and_wait(
            f"select {sql} from unnest([st_geogpoint(1, 2), "
            "st_geogpoint(1.0000000000000002, 2), st_geogpoint(1, 2)]) as g with offset o order by o"
        )
    ]
    assert got[0] == got[2] != got[1]


@pytest.mark.bigquery
def test_bigquery_arrays_match_duckdb_lists_and_the_plants_are_found(
    bigquery_url, client, tmp_path
):
    """ARRAY<STRING>, ARRAY<INT64>, ARRAY<DATE> against DuckDB lists: equal
    rows match (the shared LIST rule, checked by the sample too) and exactly
    the three planted changes are found."""
    from conftest import duckdb_write, open_bigquery, open_duckdb

    from parity.engine import diff

    c, project, dataset, made = client
    t = f"rthfix_bq_lists_{RUN}"
    made.append(t)
    c.query_and_wait(
        f"create or replace table `{project}`.`{dataset}`.`{t}` as "
        "select * from unnest(["
        "struct(1 as id, ['a, b'] as s, [1, 2] as n, [date '2024-02-29'] as d), "
        "struct(2, ['a', 'b'], cast([] as array<int64>), cast([] as array<date>)), "
        "struct(3, [''], [7], [date '0001-01-01']), "
        "struct(4, ['x'], [1], [date '2024-01-01']), "
        "struct(5, cast([] as array<string>), [5, 6], [date '9999-12-31'])])"
    )
    path = str(tmp_path / "l.duckdb")
    con = duckdb_write(path)
    try:
        con.execute("create table t (id bigint, s varchar[], n bigint[], d date[])")
        con.execute(
            "insert into t values "
            "(1, ['a, b'], [1, 2], ['2024-02-29']), "
            "(2, ['a', 'b'], [], []), "
            "(3, [], [7], ['0001-01-01']), "  # [''] against [] - planted
            "(4, ['x'], [2], ['2024-01-01']), "  # [1] against [2] - planted
            "(5, [], [6, 5], ['9999-12-31'])"  # order is part of the value - planted
        )
    finally:
        con.close()
    a, b = open_bigquery(bigquery_url, "A"), open_duckdb(path, "B")
    try:
        result = diff(a, b, t, "t", key="id", threshold=2, bisection_factor=4)
    finally:
        a.close()
        b.close()
    got = [(d.key, d.kind, sorted(d.columns or [])) for d in result.diffs]
    assert got == [
        (3, "different", ["s"]),
        (4, "different", ["n"]),
        (5, "different", ["n"]),
    ]
