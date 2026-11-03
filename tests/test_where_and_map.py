"""Row filters (`--where`) and column renames (`--map`), with no database.

Every test plants a difference and asserts exactly what is found. The two
dangers here are both false matches: a filter that misses one query (a row
the walk saw and the counts did not, or the reverse), and a renamed column
compared against the wrong counterpart.
"""

from __future__ import annotations

import io
import json
import re

import pytest
from fakes import DictTable, FakeDialect, SyntheticTable

from parity.cli import render_human, to_dict
from parity.dialects.base import row_filter_problem
from parity.engine import diff
from parity.types import Column, LogicalType

ROW_READS = {"key_stats", "segment_checksums", "fetch_range", "fetch_sample"}


def even(k: int) -> bool:
    """The stand-in predicate: even keys only."""
    return k % 2 == 0


def kinds(result) -> list[tuple[int, str]]:
    """Every difference as a sorted (key, kind) list."""
    return sorted((d.key, d.kind) for d in result.diffs)


def filtered_pair(table_a, table_b, where_a="even", where_b="even", **kwargs):
    """Diff two fakes through the `even` filter; return result and sides."""
    a = FakeDialect(table_a, side="A", predicates={"even": even})
    b = FakeDialect(table_b, side="B", predicates={"even": even})
    result = diff(a, b, "a.t", "b.t", "id", where_a=where_a, where_b=where_b, **kwargs)
    return result, a, b


# ---------------------------------------------------------------------------
# 1. The filter reaches every query that reads rows
# ---------------------------------------------------------------------------


def test_every_row_reading_call_receives_the_filter():
    """key stats, every checksum level, every download and the sample: all
    filtered. A planted difference makes the walk reach the download."""
    n = 50_000
    result, a, b = filtered_pair(
        SyntheticTable(n), SyntheticTable(n, changed={1234: ("0.00", "x")}), threshold=50
    )
    assert kinds(result) == [(1234, "different")]
    for side in (a, b):
        methods = {m for m, _ in side.wheres}
        assert methods == ROW_READS, methods
        assert all(w == "even" for _, w in side.wheres), side.wheres
    assert result.where_a == result.where_b == "even"


def test_an_unfiltered_run_passes_no_filter_at_all():
    """No filter: the dialect calls are exactly the ones they always were."""
    a = FakeDialect(SyntheticTable(1000), side="A")
    b = FakeDialect(SyntheticTable(1000, changed={7: ("0.00", "x")}), side="B")
    result = diff(a, b, "a.t", "b.t", "id")
    assert kinds(result) == [(7, "different")]
    assert all(w is None for _, w in a.wheres + b.wheres)
    assert not result.filtered


def test_a_difference_outside_the_filter_is_not_reported_and_the_result_says_filtered():
    """Key 7 (odd) differs but the filter keeps even keys: not compared, so
    identical - over the filtered rows, which the result states."""
    result, _, _ = filtered_pair(
        SyntheticTable(1000), SyntheticTable(1000, changed={7: ("0.00", "x")})
    )
    assert result.diffs == []
    assert result.identical
    assert result.filtered
    assert result.stats.rows_compared_a == 500
    assert any("only the rows it selects are compared" in w for w in result.warnings)


def test_a_difference_inside_the_filter_is_found():
    """Key 8 (even) differs: found, exactly."""
    result, _, _ = filtered_pair(
        SyntheticTable(1000), SyntheticTable(1000, changed={8: ("0.00", "x")})
    )
    assert kinds(result) == [(8, "different")]


def test_a_filter_that_drops_a_row_on_one_side_only_is_only_in():
    """Side B's filter also drops key 10: reported as only in A."""
    a = FakeDialect(SyntheticTable(100), side="A", predicates={"even": even})
    b = FakeDialect(
        SyntheticTable(100),
        side="B",
        predicates={"even but 10": lambda k: even(k) and k != 10},
    )
    result = diff(a, b, "a.t", "b.t", "id", where_a="even", where_b="even but 10")
    assert kinds(result) == [(10, "only_in_a")]


def test_filtering_one_side_only_warns_that_the_other_reads_whole():
    """Only A filtered: B's odd keys read as only in B, and a warning says why."""
    a = FakeDialect(SyntheticTable(20), side="A", predicates={"even": even})
    b = FakeDialect(SyntheticTable(20), side="B")
    result = diff(a, b, "a.t", "b.t", "id", where_a="even")
    assert kinds(result) == [(k, "only_in_b") for k in range(1, 21, 2)]
    assert any("only side A is filtered" in w for w in result.warnings)
    assert result.where_b is None


class UnstableFilterDialect(FakeDialect):
    """A filter that drops key `gone` from the `from_query`-th row read on,
    like a predicate on random() or clock_timestamp(), or on now() where an
    engine gives every query its own snapshot."""

    def __init__(self, *args, gone: int, from_query: int, **kwargs):
        """Hold which key leaves the filter, and when."""
        super().__init__(*args, **kwargs)
        self.reads = 0
        self.predicates["unstable"] = lambda k: k != gone or self.reads < from_query

    def _keep(self, method, where):
        """Count row reads, so the filter moves at a known query."""
        self.reads += 1
        return super()._keep(method, where)


def unstable_pair(gone: int, from_query: int, changed: int | None = None, **kwargs):
    """Two fakes whose filter drops `gone` from read `from_query` on; side B
    differs at `changed`."""
    plant = {} if changed is None else {changed: ("0.00", "planted")}
    a = UnstableFilterDialect(
        SyntheticTable(1000), side="A", gone=gone, from_query=from_query
    )
    b = UnstableFilterDialect(
        SyntheticTable(1000, changed=plant), side="B", gone=gone, from_query=from_query
    )
    return diff(
        a, b, "a.t", "b.t", "id", where_a="unstable", where_b="unstable", **kwargs
    )


@pytest.mark.sample_check_may_disagree
def test_a_row_leaving_the_filter_after_the_row_count_is_never_identical():
    """Key 400 leaves between the row count and the first checksum level:
    the level's counts no longer add up to the row count."""
    result = unstable_pair(gone=400, from_query=2)
    assert not result.identical
    assert any("checksums covered" in u for u in result.unexplained), result.unexplained


@pytest.mark.sample_check_may_disagree
def test_a_difference_leaving_the_filter_between_levels_is_never_identical():
    """The trap the per-level check closes: keys 400 differs, the first level
    sees it, and then it leaves the filter on both sides before the second
    level looks. The second level's buckets match, so without the check that
    a range's buckets add up to what the level above measured, the
    difference vanished and the run read identical."""
    result = unstable_pair(gone=400, from_query=3, changed=400, threshold=5)
    assert not result.identical
    assert any("at the next add up to" in u for u in result.unexplained), (
        result.unexplained
    )


def test_a_stable_filter_on_the_same_data_is_identical_and_finds_a_plant():
    """The control: the same walk with a filter that does not move adds up at
    every level, finds the planted difference and nothing else."""
    result = unstable_pair(gone=400, from_query=10**9, changed=401, threshold=5)
    assert kinds(result) == [(401, "different")]
    assert not result.unexplained


def test_a_refused_filter_runs_no_query():
    """A predicate that could end or escape its statement is refused before
    anything is read, naming the side."""
    a = FakeDialect(SyntheticTable(10), side="A")
    b = FakeDialect(SyntheticTable(10), side="B")
    with pytest.raises(ValueError, match=r"\[side B: fake\] row filter"):
        diff(a, b, "a.t", "b.t", "id", where_b="1=1) or (1=1")
    assert a.queries == b.queries == 0


@pytest.mark.parametrize(
    ("predicate", "why"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("x = 1; drop table t", "';'"),
        ("x = 1 -- and y", "'--'"),
        ("x = 1 /* c */", "'/\\*'"),
        ("x = 1 # c", "'#'"),
        ("1=1) or (1=1", "never opened"),
        ("(x = 1", "unclosed"),
        ("x = 'a", "unterminated"),
        ('x = "a', "unterminated"),
        ("x = 'a\\''", "backslash before a quote"),
        ("x = '''a'''", "triple-quoted"),
        ("x = $$a$$", "dollar-quoted"),
        ("x = $t$a$t$", "dollar-quoted"),
        ("[a'b] ) or (1=1 or [c'd]", "never opened"),
        ("x = 1\x00", "control character"),
    ],
)
def test_predicates_that_could_end_or_escape_the_statement_are_refused(predicate, why):
    """Each refusal names its reason."""
    problem = row_filter_problem(predicate)
    assert problem is not None
    assert re.search(why, problem), problem


@pytest.mark.parametrize(
    "predicate",
    [
        "x > 1",
        "created_at >= '2026-10-01' and region in ('eu', 'ch')",
        "name = 'it''s'",
        "s = 'a#b'",
        "a[1] > 2",
        "[my col] = 1",
        "[a]]b] = 1",
        'x = "quoted"',
        "regexp_like(s, '\\d+')",
        "coalesce(x, 0) between 1 and 10\nand y is not null",
        "(a or b) and (c or d)",
        "col$1 > 0",
    ],
)
def test_ordinary_predicates_are_accepted(predicate):
    """What people actually write passes."""
    assert row_filter_problem(predicate) is None


def test_rows_from_parenthesises_the_filter_and_ands_it_first():
    """`a or b` cannot widen the bucket bounds it is ANDed with."""
    d = FakeDialect(SyntheticTable(1), side="A")
    assert d.rows_from("t", None) == 'from "t"'
    assert d.rows_from("t", None, "k >= 1") == 'from "t" where k >= 1'
    assert d.rows_from("t", " a or b ", "k >= 1", "k <= 9") == (
        'from "t" where (a or b) and k >= 1 and k <= 9'
    )


# ---------------------------------------------------------------------------
# 2. Reporting a filtered run
# ---------------------------------------------------------------------------


def test_a_filtered_report_states_each_sides_filter_in_both_formats():
    """Human: headline and a `filtered:` line. JSON: where_a/where_b, and the
    risk figures marked as of the filtered rows."""
    a = FakeDialect(SyntheticTable(200), side="A", predicates={"k % 2 = 0": even})
    b = FakeDialect(SyntheticTable(200), side="B")
    result = diff(a, b, "a.t", "b.t", "id", where_a="k % 2 = 0")
    out = io.StringIO()
    render_human(result, out)
    text = out.getvalue()
    assert "(filtered rows only)" in text.splitlines()[0]
    assert "filtered: A where k % 2 = 0; B whole table" in text
    payload = json.loads(json.dumps(to_dict(result)))
    assert payload["where_a"] == "k % 2 = 0"
    assert payload["where_b"] is None
    assert payload["risk"]["filtered"] is True


def test_an_identical_filtered_run_says_filtered_in_the_risk_line():
    """The risk line's counts are of the filtered rows, and it says so."""
    result, _, _ = filtered_pair(SyntheticTable(400), SyntheticTable(400))
    out = io.StringIO()
    render_human(result, out)
    text = out.getvalue()
    assert result.identical
    assert "no differences in 200 rows (filtered rows only)" in text
    assert "every filtered row was compared" in text


# ---------------------------------------------------------------------------
# 3. Column renames
# ---------------------------------------------------------------------------

A_COLS = [
    Column("amount", LogicalType.DECIMAL, "decimal(12,2)"),
    Column("status", LogicalType.STRING, "varchar"),
]
B_COLS = [
    Column("amt", LogicalType.DECIMAL, "decimal(12,2)"),
    Column("status", LogicalType.STRING, "varchar"),
]


def mapped(table_a, table_b, column_map, key="id", b_key=None, **kwargs):
    """Diff A against B through a column map."""
    a = FakeDialect(table_a, side="A")
    kwargs_b = {} if b_key is None else {"key_type_override": b_key}
    b = FakeDialect(table_b, side="B", **kwargs_b)
    return diff(a, b, "a.t", "b.t", key, column_map=column_map, **kwargs)


def test_a_renamed_column_is_compared_and_a_planted_change_found():
    """`amount` on A is `amt` on B; key 42's amount differs: found, under A's
    name, with B's name in the result's map."""
    result = mapped(
        SyntheticTable(1000, columns=A_COLS),
        SyntheticTable(1000, columns=B_COLS, changed={42: ("1.00", "status-0")}),
        {"amount": "amt"},
    )
    assert kinds(result) == [(42, "different")]
    assert result.diffs[0].columns == ["amount"]
    assert result.column_map == {"amount": "amt"}
    assert not result.schema_differences


def test_without_the_map_the_rename_is_a_schema_difference():
    """The same tables unmapped: never identical."""
    a = FakeDialect(SyntheticTable(10, columns=A_COLS), side="A")
    b = FakeDialect(SyntheticTable(10, columns=B_COLS), side="B")
    result = diff(a, b, "a.t", "b.t", "id")
    assert not result.identical
    assert result.schema_differences


def test_the_map_is_case_insensitive_like_all_matching():
    """`AMOUNT=AMT` pairs `amount` with `amt`."""
    result = mapped(
        SyntheticTable(100, columns=A_COLS),
        SyntheticTable(100, columns=B_COLS, changed={5: ("9.99", "status-5")}),
        {"AMOUNT": "AMT"},
    )
    assert kinds(result) == [(5, "different")]
    assert result.column_map == {"amount": "amt"}


def test_a_renamed_key_column_works():
    """`--key id` names A's column; B calls it `order_id`. A deleted row and a
    changed one are both found."""
    b_key = Column("order_id", LogicalType.INTEGER, "bigint")
    result = mapped(
        SyntheticTable(500, columns=A_COLS),
        SyntheticTable(
            500, columns=A_COLS, deleted=[9], changed={100: ("0.00", "status-2")}
        ),
        {"id": "order_id"},
        b_key=b_key,
    )
    assert kinds(result) == [(9, "only_in_a"), (100, "different")]
    assert result.column_map == {"id": "order_id"}


SWAP_A = [
    Column("first", LogicalType.STRING, "varchar"),
    Column("last", LogicalType.STRING, "varchar"),
]


def swap_rows(n: int, swapped: bool, plant: int | None = None):
    """Rows whose two text columns differ, optionally stored swapped."""
    rows = {}
    for k in range(1, n + 1):
        first, last = f"f{k}", f"l{k}"
        if plant == k:
            first = "planted"
        rows[k] = (last, first) if swapped else (first, last)
    return DictTable(SWAP_A, rows)


def test_two_columns_mapped_onto_each_other_pair_by_the_map_not_by_name():
    """B stores `first` in its `last` column and vice versa; mapped as a swap,
    the tables match - except key 3, planted on B, which is found."""
    result = mapped(
        swap_rows(50, swapped=False),
        swap_rows(50, swapped=True, plant=3),
        {"first": "last", "last": "first"},
    )
    assert kinds(result) == [(3, "different")]
    assert result.diffs[0].columns == ["first"]
    assert result.column_map == {"first": "last", "last": "first"}


def test_a_swap_map_over_unswapped_data_reports_every_row():
    """The ordering trap, the other way round: B is *not* swapped but the map
    says it is. Pairing by name would call these identical - a false match
    against what the user declared. Every row must differ, in both columns."""
    result = mapped(
        swap_rows(30, swapped=False),
        swap_rows(30, swapped=False),
        {"first": "last", "last": "first"},
    )
    assert len(result.diffs) == 30
    assert all(sorted(d.columns) == ["first", "last"] for d in result.diffs)


@pytest.mark.parametrize(
    ("column_map", "match"),
    [
        ({"amonut": "amt"}, r"\[side A\] --map names column 'amonut'.*'amount'"),
        ({"amount": "amnt"}, r"\[side B\] --map names column 'amnt'.*'amt'"),
        ({"amount": "amt", "AMOUNT": "status"}, "two side B columns"),
        ({"amount": "amt", "status": "AMT"}, "two side A columns"),
        # B's `status` matches A's `status` by name, and the map gives it to
        # A's `amount` as well.
        ({"amount": "status"}, "side A also has a column 'status'"),
    ],
)
def test_ambiguous_or_unknown_mappings_are_refused(column_map, match):
    """Each refusal names the side and the column."""
    with pytest.raises(ValueError, match=match):
        mapped(
            SyntheticTable(10, columns=A_COLS),
            SyntheticTable(10, columns=B_COLS),
            column_map,
        )


def test_a_b_column_matched_by_name_and_claimed_by_the_map_is_refused():
    """B has both `amount` and `amt`; mapping A's amount to amt leaves B's
    amount matching A's amount by name too."""
    b_cols = [*B_COLS, Column("amount", LogicalType.DECIMAL, "decimal(12,2)")]

    class Wide(SyntheticTable):
        def _generated(self, key):
            """The usual row, plus the third column side B carries."""
            return (*super()._generated(key), "0.00")

    with pytest.raises(ValueError, match="side B also has a column 'amount'"):
        mapped(
            SyntheticTable(10, columns=A_COLS),
            Wide(10, columns=b_cols),
            {"amount": "amt"},
        )


def test_mapping_and_excluding_one_column_is_refused():
    """By A's name: contradictory. By B's name: --exclude takes A's names."""
    a_t, b_t = SyntheticTable(10, columns=A_COLS), SyntheticTable(10, columns=B_COLS)
    with pytest.raises(ValueError, match="--map and --exclude both name 'amount'"):
        mapped(a_t, b_t, {"amount": "amt"}, exclude=["amount"])
    with pytest.raises(ValueError, match="--exclude names 'amt', side B's name"):
        mapped(a_t, b_t, {"amount": "amt"}, exclude=["amt"])
    with pytest.raises(ValueError, match="--columns names 'amt', side B's name"):
        mapped(a_t, b_t, {"amount": "amt"}, columns=["amt"])


def test_columns_takes_the_a_side_name_of_a_mapped_column():
    """`--columns amount` compares the renamed column only, and finds its
    planted change; the status change is outside the selection."""
    result = mapped(
        SyntheticTable(100, columns=A_COLS),
        SyntheticTable(
            100, columns=B_COLS, changed={4: ("0.00", "status-4"), 6: ("6.06", "zzz")}
        ),
        {"amount": "amt"},
        columns=["amount"],
    )
    assert kinds(result) == [(4, "different")]


def test_a_mapped_difference_names_both_sides_in_both_formats():
    """Human: `amount (B: amt)`. JSON: `column_map` and per-row `b_columns`."""
    result = mapped(
        SyntheticTable(100, columns=A_COLS),
        SyntheticTable(100, columns=B_COLS, changed={42: ("1.00", "status-0")}),
        {"amount": "amt"},
    )
    out = io.StringIO()
    render_human(result, out)
    text = out.getvalue()
    assert "amount (B: amt)" in text
    assert "renamed columns (A = B): amount = B amt" in text
    payload = to_dict(result)
    assert payload["column_map"] == {"amount": "amt"}
    assert payload["differences"][0]["columns"] == ["amount"]
    assert payload["differences"][0]["b_columns"] == ["amt"]
