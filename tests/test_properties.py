"""Property-based, oracle, and metamorphic tests for the bisection engine.

Everything here is generative: Hypothesis builds thousands of tables and the
tests assert properties that must hold for *all* of them, rather than for a few
hand-picked cases. The centrepiece is the **oracle** - parity's segmented,
network-frugal diff is checked against a trivial "download everything and
compare in Python" reference over random inputs. Any logic error in the walk,
the bucket arithmetic, or the row comparison shows up as a disagreement with
the oracle, on an input no human thought to write down.

All of it runs against the in-memory `FakeDialect`, so it needs no database and
runs everywhere - and `FakeDialect.query` raises, so these also keep proving the
engine never reaches past the dialect contract to build SQL itself.
"""

from __future__ import annotations

import pytest

# Hypothesis is a test-only dependency (the `test` extra). If it is somehow
# absent, skip this whole module rather than fail collection - a missing test
# dependency must not look like a broken build.
pytest.importorskip("hypothesis")

from conftest import deep_examples
from fakes import DictTable, FakeDialect
from hypothesis import given, settings
from hypothesis import strategies as st

from parity.engine import bucket_bounds, diff
from parity.types import Column, LogicalType

COLS = [
    Column("a", LogicalType.STRING, "varchar"),
    Column("b", LogicalType.STRING, "varchar"),
]

# Text values that avoid the field separator (0x1f) and the NULL sentinel, so
# these tests isolate the *bisection* logic from the *encoding*. The separator
# and sentinel get their own adversarial tests in test_fuzz_encoding.py.
_safe_text = st.text(
    alphabet=st.characters(blacklist_characters="\x1f", blacklist_categories=("Cs",)),
    max_size=12,
)
_row = st.tuples(_safe_text, _safe_text)
# Keys span negatives and a wide range, so bucketing edge cases are exercised.
_key = st.integers(min_value=-(10**6), max_value=10**6)
_table = st.dictionaries(_key, _row, max_size=60)


def _oracle(rows_a: dict, rows_b: dict) -> list[tuple[int, str]]:
    """The truth, computed the dumb way: compare every row directly.

    parity must reproduce this exactly, however cleverly it gets there.
    """
    out = []
    for k in set(rows_a) | set(rows_b):
        a, b = rows_a.get(k), rows_b.get(k)
        if a is None:
            out.append((k, "only_in_b"))
        elif b is None:
            out.append((k, "only_in_a"))
        elif a != b:
            out.append((k, "different"))
    return sorted(out)


def _run(rows_a: dict, rows_b: dict, **kwargs):
    """Diff two literal row dicts through the engine, over the in-memory fake."""
    a = FakeDialect(DictTable(COLS, rows_a), side="A")
    b = FakeDialect(DictTable(COLS, rows_b), side="B")
    return diff(a, b, "a.t", "b.t", "id", **kwargs)


def _kinds(result) -> list[tuple[int, str]]:
    """The result as a sorted (key, kind) list, for comparison against the oracle."""
    return sorted((d.key, d.kind) for d in result.diffs)


# ---------------------------------------------------------------------------
# The oracle: the segmented walk must equal a brute-force comparison, always.
# ---------------------------------------------------------------------------


@settings(max_examples=deep_examples(400))
@given(a=_table, b=_table)
def test_diff_matches_a_brute_force_oracle(a, b):
    """For any two tables, parity's result equals comparing every row directly.

    This is the whole engine under test at once. A skipped range, an
    off-by-one bucket, a misclassified row - any of them makes this fail on
    some generated input.
    """
    assert _kinds(_run(a, b)) == _oracle(a, b)


@settings(max_examples=200)
@given(
    a=_table,
    b=_table,
    bisection_factor=st.integers(min_value=2, max_value=64),
    threshold=st.integers(min_value=1, max_value=50),
)
def test_result_is_invariant_to_the_tuning_knobs(a, b, bisection_factor, threshold):
    """bisection_factor and threshold change cost, never the verdict.

    Fan-out and download-threshold are performance dials. If either changes
    *which* rows are reported, the walk is wrong.
    """
    assert _kinds(_run(a, b, bisection_factor=bisection_factor, threshold=threshold)) == _oracle(a, b)


# ---------------------------------------------------------------------------
# Metamorphic properties: relations that must hold between related runs.
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(t=_table)
def test_a_table_diffed_against_itself_is_identical(t):
    """The most basic invariant, and a false match here is catastrophic."""
    result = _run(t, t)
    assert result.identical
    assert result.diffs == []
    assert result.stats.rows_downloaded == 0


@settings(max_examples=300)
@given(a=_table, b=_table)
def test_diff_is_symmetric_under_side_swap(a, b):
    """diff(A,B) and diff(B,A) are mirror images: only_in_a <-> only_in_b,
    different stays different, same keys throughout."""
    forward = dict(_kinds(_run(a, b)))
    backward = dict(_kinds(_run(b, a)))
    flip = {"only_in_a": "only_in_b", "only_in_b": "only_in_a", "different": "different"}
    assert backward == {k: flip[v] for k, v in forward.items()}


@settings(max_examples=200)
@given(t=_table, extra=_table)
def test_rows_downloaded_is_zero_exactly_when_identical(t, extra):
    """The core efficiency promise, stated as an iff over random inputs."""
    same = _run(t, t)
    assert same.stats.rows_downloaded == 0 and same.identical

    other = _run(t, extra)
    if _oracle(t, extra):  # they genuinely differ
        assert other.stats.rows_downloaded > 0 and not other.identical
    else:
        assert other.stats.rows_downloaded == 0 and other.identical


@settings(max_examples=200)
@given(
    base=st.dictionaries(_key, _row, min_size=1, max_size=40),
    changed=st.integers(min_value=0, max_value=39),
)
def test_exactly_n_changed_rows_are_found(base, changed):
    """Change a chosen number of rows; the walk must find exactly that many."""
    keys = sorted(base)
    n = min(changed, len(keys))
    b = dict(base)
    for k in keys[:n]:
        b[k] = (base[k][0] + "X", base[k][1])  # guaranteed different
    result = _run(base, b)
    assert [d.kind for d in result.diffs] == ["different"] * n
    assert {d.key for d in result.diffs} == set(keys[:n])


@settings(max_examples=150)
@given(a=_table, b=_table)
def test_excluding_every_differing_column_yields_identical(a, b):
    """Metamorphic: a diff driven only by column values disappears when those
    columns are excluded. Only key-presence differences can remain."""
    result = _run(a, b, exclude=["a", "b"])
    presence_only = sorted(
        (k, kind) for k, kind in _oracle(a, b) if kind != "different"
    )
    assert _kinds(result) == presence_only


# ---------------------------------------------------------------------------
# bucket_bounds: the inverse of the SQL bucket expression, over the whole space.
# ---------------------------------------------------------------------------


def _sql_bucket(key: int, lo: int, hi: int, n: int) -> int:
    """The SQL bucket expression in Python: which segment a key falls in."""
    return ((key - lo) * n) // (hi - lo)


@settings(max_examples=500)
@given(
    lo=st.integers(min_value=-(10**9), max_value=10**9),
    span=st.integers(min_value=2, max_value=10**7),
    n=st.integers(min_value=2, max_value=1000),
)
def test_bucket_bounds_tiles_the_range_with_no_gap_or_overlap(lo, span, n):
    """Every key lands in exactly one bucket, and the buckets cover [lo, hi).

    A gap is a row the walker never looks at while still reporting a clean
    match - the worst failure this tool can have. Tested over the full space
    of (lo, span, n), not a handful of seeds.
    """
    hi = lo + span
    n = min(n, span)
    prev_hi = lo
    for i in range(n):
        b_lo, b_hi = bucket_bounds(i, lo, hi, n)
        assert b_lo == prev_hi, f"gap or overlap before bucket {i}"
        assert b_lo <= b_hi
        prev_hi = b_hi
    assert prev_hi == hi, "buckets do not reach the end of the range"


@settings(max_examples=300)
@given(
    lo=st.integers(min_value=-1000, max_value=1000),
    span=st.integers(min_value=2, max_value=3000),
    n=st.integers(min_value=2, max_value=200),
    offset=st.integers(min_value=0),
)
def test_bucket_bounds_agrees_with_the_sql_expression_for_a_key(lo, span, n, offset):
    """For a key in range, Python's bucket assignment matches the SQL formula.

    The two must agree exactly, or the walker recurses into the wrong range.
    """
    hi = lo + span
    n = min(n, span)
    key = lo + (offset % span)
    sql = _sql_bucket(key, lo, hi, n)
    b_lo, b_hi = bucket_bounds(sql, lo, hi, n)
    assert b_lo <= key < b_hi


# ---------------------------------------------------------------------------
# max_diffs: a capped run is a prefix of the truth, never a clean bill.
# ---------------------------------------------------------------------------


@settings(max_examples=150)
@given(
    base=st.dictionaries(_key, _row, min_size=1, max_size=40),
    limit=st.integers(min_value=1, max_value=20),
)
def test_max_diffs_never_reports_identical_when_differences_exist(base, limit):
    """Change every row, cap the report, and check the cap is honest.

    A truncated run must carry the flag, must never read as identical, and must
    return no more than the limit.
    """
    b = {k: (v[0] + "Z", v[1]) for k, v in base.items()}
    result = _run(base, b, max_diffs=limit)

    assert len(result.diffs) <= limit
    if len(base) > limit:
        assert result.truncated
        assert not result.identical
