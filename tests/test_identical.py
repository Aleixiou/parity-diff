"""The identical check, stress-tested from every offline angle.

A parity tool that reports a false match is worse than useless (CLAUDE.md 8), so
the property that identical tables report identical - and that identical-except-
one-thing never does - deserves its own dedicated hunt. These run against the
in-memory `FakeDialect`, so they exercise the *bisection, matching and checksum*
logic at high volume with hostile data; the cross-engine *encoding* side of the
same promise lives in test_encoding.py and the live suites.

Two deliberate choices about the field separator (0x1f):
- The "identical stays identical" tests include it in the data on purpose - the
  same bytes on both sides must compare equal however hostile.
- The false-identical hunter excludes it, because `concat_ws` smearing on a
  separator that appears in real data is a documented limitation (CLAUDE.md
  4.3), not an abnormality, and planting it would test the wrong thing.
"""

from __future__ import annotations

import pytest

pytest.importorskip("hypothesis")

from fakes import DictTable, FakeDialect, SyntheticTable
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from parity.engine import diff
from parity.types import Column, LogicalType

# Hostile text: full Unicode, control characters, even NUL - the fake is pure
# Python, so the point is that identical *bytes* stay identical however ugly.
# Surrogates are excluded only because they have no encoding at all.
_FULL = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)), max_size=18
)
# The same, minus the field separator, for tests that plant a difference.
_SAFE = st.text(
    alphabet=st.characters(blacklist_characters="\x1f", blacklist_categories=("Cs",)),
    max_size=18,
)
_KEY = st.integers(min_value=-(10**12), max_value=10**12)


def _table(text=_SAFE, min_rows=0, max_cols=6):
    """A strategy for (columns, rows): 1..max_cols string columns, sparse keys."""

    @st.composite
    def build(draw):
        """Draw one (columns, rows) pair for the strategy above."""
        ncols = draw(st.integers(min_value=1, max_value=max_cols))
        cols = [Column(f"c{i}", LogicalType.STRING, "varchar") for i in range(ncols)]
        keys = draw(
            st.lists(_KEY, unique=True, min_size=min_rows, max_size=30)
        )
        rows = {k: tuple(draw(text) for _ in range(ncols)) for k in keys}
        return cols, rows

    return build()


def _oracle(rows_a: dict, rows_b: dict) -> list[tuple[int, str]]:
    """The truth, computed the dumb way, for the change hunter."""
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


def _run(cols_a, rows_a, cols_b, rows_b, **kwargs):
    """Diff two explicit (columns, rows) sides through the engine."""
    a = FakeDialect(DictTable(cols_a, rows_a), side="A")
    b = FakeDialect(DictTable(cols_b, rows_b), side="B")
    return diff(a, b, "a.t", "b.t", "id", **kwargs)


def _kinds(result) -> list[tuple[int, str]]:
    """Result as a sorted (key, kind) list."""
    return sorted((d.key, d.kind) for d in result.diffs)


# ---------------------------------------------------------------------------
# Identical stays identical - however hostile the data, whatever the knobs.
# ---------------------------------------------------------------------------


@settings(max_examples=500)
@given(
    data=_table(text=_FULL),
    bisection_factor=st.integers(min_value=2, max_value=64),
    threshold=st.integers(min_value=1, max_value=100),
)
def test_identical_tables_are_identical_under_every_knob(data, bisection_factor, threshold):
    """A table against itself: identical, no diffs, zero rows moved - for any
    fan-out and threshold, and even with separators and NULs in the data."""
    cols, rows = data
    result = _run(
        cols, rows, cols, dict(rows),
        bisection_factor=bisection_factor, threshold=threshold,
    )
    assert result.identical
    assert result.diffs == []
    assert result.stats.rows_downloaded == 0


@settings(max_examples=300)
@given(data=_table(text=_FULL, min_rows=1))
def test_identical_downloads_zero_and_costs_four_queries(data):
    """The headline efficiency claim on any non-empty table: 4 queries
    (2 key_stats + 2 first-level checksums) and nothing downloaded."""
    cols, rows = data
    result = _run(cols, rows, cols, dict(rows))
    assert result.identical
    assert result.stats.rows_downloaded == 0
    assert result.stats.queries == 4


@settings(max_examples=300)
@given(data=_table(text=_FULL, min_rows=1), perm=st.randoms(use_true_random=False))
def test_identical_is_independent_of_column_order(data, perm):
    """Columns are matched by name, so the same data with columns in a different
    order on side B still compares identical - order is not content."""
    cols, rows = data
    order = list(range(len(cols)))
    perm.shuffle(order)
    cols_b = [cols[i] for i in order]
    rows_b = {k: tuple(v[i] for i in order) for k, v in rows.items()}
    result = _run(cols, rows, cols_b, rows_b)
    assert result.identical
    assert result.stats.rows_downloaded == 0


@settings(max_examples=100, deadline=None)  # large-table walks are legitimately slow
@given(n=st.integers(min_value=1, max_value=200_000), bf=st.integers(min_value=2, max_value=64))
def test_identical_at_scale_downloads_nothing(n, bf):
    """Two identical generated tables of up to 200k rows: still zero download,
    and the query count stays tiny (a logarithmic walk that never recurses)."""
    a = FakeDialect(SyntheticTable(n), side="A")
    b = FakeDialect(SyntheticTable(n), side="B")
    result = diff(a, b, "t", "t", "id", bisection_factor=bf)
    assert result.identical
    assert result.stats.rows_downloaded == 0
    assert result.stats.queries == 4


@settings(max_examples=200)
@given(data=_table(text=_FULL))
def test_the_identical_check_is_deterministic(data):
    """Running the identical check twice gives the same verdict and counts -
    no dependence on dict ordering, threads, or hash seeding."""
    cols, rows = data
    first = _run(cols, rows, cols, dict(rows))
    second = _run(cols, rows, cols, dict(rows))
    assert first.identical == second.identical
    assert _kinds(first) == _kinds(second)
    assert first.stats.rows_downloaded == second.stats.rows_downloaded
    assert first.stats.queries == second.stats.queries


# ---------------------------------------------------------------------------
# The false-identical hunter: one minimal change must never read as identical.
# ---------------------------------------------------------------------------


@settings(max_examples=600)
@given(data=_table(text=_SAFE, min_rows=1), seed=st.randoms(use_true_random=False), newval=_SAFE)
def test_a_single_planted_change_is_never_called_identical(data, seed, newval):
    """Take an identical pair, apply exactly one change - alter a cell, delete a
    row, or insert a row - and assert the walk never reports identical and finds
    exactly what a brute-force comparison would. This is the failure the whole
    tool exists to prevent, concentrated onto the hardest case: one difference.
    """
    cols, rows = data
    rows_b = dict(rows)
    keys = sorted(rows)
    kind = seed.choice(["change", "delete", "insert"])
    if kind == "change":
        k = seed.choice(keys)
        col = seed.randrange(len(cols))
        v = list(rows_b[k])
        v[col] = newval
        rows_b[k] = tuple(v)
    elif kind == "delete":
        del rows_b[seed.choice(keys)]
    else:  # insert a key not already present
        newk = seed.randint(-(10**12), 10**12)
        assume(newk not in rows_b)
        rows_b[newk] = tuple(newval for _ in cols)

    assume(rows_b != rows)  # a change that changed nothing is not a test

    result = _run(cols, rows, cols, rows_b)
    assert not result.identical, "a real difference was reported as identical"
    assert _kinds(result) == _oracle(rows, rows_b)
    assert result.stats.rows_downloaded > 0


@settings(max_examples=300)
@given(data=_table(text=_SAFE, min_rows=1), seed=st.randoms(use_true_random=False))
def test_null_versus_value_on_one_cell_is_found(data, seed):
    """The migration bug class: a value on one side, absent (a different value)
    on the other, in a single cell, must be caught - never smoothed to a match.
    """
    cols, rows = data
    k = seed.choice(sorted(rows))
    col = seed.randrange(len(cols))
    v = list(rows[k])
    # Flip the cell to something guaranteed different from what is there.
    v[col] = v[col] + "␀" if v[col] != "␀" else "x"
    rows_b = dict(rows)
    rows_b[k] = tuple(v)
    result = _run(cols, rows, cols, rows_b)
    assert not result.identical
    assert _kinds(result) == [(k, "different")]
    assert result.diffs[0].columns == [f"c{col}"]
