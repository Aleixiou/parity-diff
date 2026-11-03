"""The independent sample check, against the in-memory fake.

The checksum walk can only be as right as each dialect's canonical-text SQL.
Every false match this tool has shipped was a rendering that made two
different values print alike, so both sides agreed on the wrong text and no
checksum could see it. The sample check re-compares rows from raw values by
the documented rules in Python; these tests plant exactly that kind of bug
and require the check to catch it - and require it to stay quiet when the
walk is right.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fakes import DictTable, FakeDialect, SyntheticTable

from parity.engine import diff
from parity.reference import UNCHECKED, reference_token
from parity.types import Column, LogicalType

COLS = [
    Column("amount", LogicalType.DECIMAL, "decimal(12,2)"),
    Column("status", LogicalType.STRING, "varchar"),
]


class LyingRender(FakeDialect):
    """A side whose rendering hides a change: the walk sees the original text
    for every key, while the raw values carry the planted change. That is
    precisely a dialect bug that renders two different values alike."""

    def __init__(self, table, hidden: dict[int, tuple], **kw):
        """`hidden` maps a key to the raw values the rendering hides."""
        super().__init__(table, **kw)
        self.hidden = hidden

    def raw_row(self, key: int) -> tuple:
        """The raw values, with the hidden change."""
        return self.hidden.get(key, super().raw_row(key))


def test_identical_tables_pass_the_check_and_say_so():
    """No false failure: identical data, every sampled row agrees."""
    a = FakeDialect(SyntheticTable(5_000, columns=COLS), side="A")
    b = FakeDialect(SyntheticTable(5_000, columns=COLS), side="B")
    result = diff(a, b, "t", "t", "id")
    v = result.verification
    assert result.identical
    assert v is not None and not v.failures and not v.error
    assert v.modulus == 5 and 0 < v.rows_a == v.rows_b < 5_000
    assert v.checked == ["amount", "status"] and v.unchecked == []
    # The walk's own counts are untouched by the check.
    assert result.stats.rows_downloaded == 0
    assert result.stats.sample_queries == 2
    assert result.stats.rows_sampled == v.rows_a + v.rows_b


def test_a_small_table_is_compared_in_full():
    """At or below the sample size every row is re-compared (modulus 1)."""
    a = FakeDialect(SyntheticTable(300, columns=COLS), side="A")
    b = FakeDialect(SyntheticTable(300, columns=COLS), side="B")
    v = diff(a, b, "t", "t", "id").verification
    assert v is not None and v.modulus == 1 and v.rows_a == v.rows_b == 300


@pytest.mark.sample_check_may_disagree
def test_a_rendering_bug_that_hides_a_change_is_caught():
    """The walk reads identical - the rendering lied - but the raw values
    differ, so the result must not be identical and must name the row."""
    a = FakeDialect(SyntheticTable(200, columns=COLS), side="A")
    b = LyingRender(
        SyntheticTable(200, columns=COLS), hidden={42: ("42.43", "status-0")}, side="B"
    )
    result = diff(a, b, "t", "t", "id")
    assert result.diffs == []  # the walk was fooled
    assert not result.identical  # the check was not
    (failure,) = result.verification.failures
    assert "key 42" in failure and "amount" in failure and "'42.430000'" in failure


def test_a_hidden_change_only_counts_where_the_rules_see_one():
    """A raw difference past the float scale is invisible by the documented
    rules too, so a rendering that hides it is right, not a bug."""
    a = FakeDialect(SyntheticTable(200, columns=COLS), side="A")
    b = LyingRender(
        SyntheticTable(200, columns=COLS),
        hidden={42: ("42.4200001", "status-0")},
        side="B",
    )
    result = diff(a, b, "t", "t", "id")
    assert result.identical and not result.verification.failures


def test_a_real_difference_found_by_the_walk_agrees_with_the_check():
    """Planted for real, found by the walk: the check confirms it."""
    a = FakeDialect(SyntheticTable(200, columns=COLS), side="A")
    b = FakeDialect(
        SyntheticTable(200, columns=COLS, changed={7: ("7.08", "status-0")}),
        side="B",
    )
    result = diff(a, b, "t", "t", "id")
    assert [(d.key, d.columns) for d in result.diffs] == [(7, ["amount"])]
    assert not result.verification.failures and not result.verification.notes


@pytest.mark.sample_check_may_disagree
def test_a_key_the_walk_missed_is_caught():
    """A row on one side only that the walk did not report - its rendering
    of the other side invented it - is a failure."""

    class Phantom(FakeDialect):
        """Side B's sample holds an extra key the walk never saw."""

        def fetch_sample(self, table, key, columns, modulus, residue):
            """The real sample plus one key the walk never saw."""
            out = super().fetch_sample(table, key, columns, modulus, residue)
            out[10_001] = ("1.00", "status-1")
            return out

    a = FakeDialect(SyntheticTable(100, columns=COLS), side="A")
    b = Phantom(SyntheticTable(100, columns=COLS), side="B")
    result = diff(a, b, "t", "t", "id")
    assert not result.identical
    assert "key 10001: in the sample of side B only" in result.verification.failures[0]


def test_a_reported_difference_the_values_do_not_show_is_only_a_note():
    """The walk reports a difference the raw values do not have - a false
    alarm, never a false match. Noted, and the verdict already says
    different, so nothing is hidden."""
    a = FakeDialect(SyntheticTable(100, columns=COLS), side="A")
    b = LyingRender(
        SyntheticTable(100, columns=COLS, changed={5: ("5.06", "status-5")}),
        hidden={5: ("5.05", "status-5")},
        side="B",
    )
    result = diff(a, b, "t", "t", "id")
    assert [d.key for d in result.diffs] == [5]
    assert not result.verification.failures
    assert "key 5: reported different in ['amount']" in result.verification.notes[0]


def test_switched_off_there_is_no_check_and_no_cost():
    """`verify_sample=0`: no sample query, no verification on the result."""
    a = FakeDialect(SyntheticTable(100, columns=COLS), side="A")
    b = FakeDialect(SyntheticTable(100, columns=COLS), side="B")
    result = diff(a, b, "t", "t", "id", verify_sample=0)
    assert result.verification is None and result.stats.sample_queries == 0
    with pytest.raises(ValueError, match="verify_sample must be >= 0"):
        diff(a, b, "t", "t", "id", verify_sample=-1)


def test_a_partial_walk_is_not_verified():
    """Past --max-diffs the walk's list is incomplete, so the sample would
    flag every unlisted difference: the check does not run."""
    a = FakeDialect(SyntheticTable(100, columns=COLS), side="A")
    b = FakeDialect(SyntheticTable(100, columns=COLS, deleted=range(1, 50)), side="B")
    result = diff(a, b, "t", "t", "id", max_diffs=5)
    assert result.truncated and result.verification is None


def test_a_check_that_cannot_run_is_reported_not_fatal():
    """A sample query that fails is printed; the walk's verdict stands."""

    class Broken(FakeDialect):
        def fetch_sample(self, *args):
            """Fail as a database error would."""
            raise self._err("sample query failed")

    a = FakeDialect(SyntheticTable(100, columns=COLS), side="A")
    b = Broken(SyntheticTable(100, columns=COLS), side="B")
    result = diff(a, b, "t", "t", "id")
    assert result.identical
    assert "sample query failed" in result.verification.error


def test_unknown_types_are_listed_as_unchecked():
    """A column the reference rules do not cover is never silently passed."""
    cols = [Column("doc", LogicalType.UNKNOWN, "json")]
    rows = {1: ('{"a": 1}',), 2: ("[]",)}
    a = FakeDialect(DictTable(cols, rows), side="A")
    b = FakeDialect(DictTable(cols, rows), side="B")
    v = diff(a, b, "t", "t", "id").verification
    assert v.checked == [] and v.unchecked == ["doc"]


# ------------------------------------------------------------ reference rules


@pytest.mark.parametrize(
    ("t", "value", "token"),
    [
        (LogicalType.INTEGER, 42, ("int", 42)),
        (LogicalType.INTEGER, Decimal("42"), ("int", 42)),
        (LogicalType.DECIMAL, Decimal("1.0000005"), ("num", "1.000001")),
        (LogicalType.DECIMAL, Decimal("-0.0000004"), ("num", "0.000000")),
        (LogicalType.FLOAT, 2.0000005, ("num", "2.000001")),
        (LogicalType.FLOAT, 1e15 + 0.5, ("num", "1000000000000000.500000")),
        (LogicalType.FLOAT, float("inf"), ("num", "Infinity")),
        (LogicalType.BOOLEAN, 1, ("bool", True)),
        (LogicalType.STRING, "a\\N\x1f", ("str", "a\\N\x1f")),
        (LogicalType.DATE, "infinity", ("date", "infinity")),
        (LogicalType.DATE, "0044-03-15 BC", ("date", "-43-03-15")),
        (
            LogicalType.TIMESTAMP,
            "2024-06-15T12:00:00+05:00",
            ("ts", "2024-06-15 07:00:00.000000"),
        ),
        (
            LogicalType.TIMESTAMP,
            "2024-01-01T00:00:00.123456789",
            ("ts", "2024-01-01 00:00:00.123456"),
        ),
    ],
)
def test_reference_tokens(t, value, token):
    """The rules on values each engine's canonical form is pinned on."""
    assert reference_token(t, value, 6) == token


def test_reference_decimal_and_float_compare_as_one_number():
    """A DECIMAL on one side and a DOUBLE on the other agree at the scale,
    as the canonical text does (CLAUDE.md 4.2)."""
    assert reference_token(
        LogicalType.DECIMAL, Decimal("2.0000005"), 6
    ) == reference_token(LogicalType.FLOAT, 2.0000005, 6)


def test_reference_char_padding_and_float4():
    """CHAR drops trailing spaces; float4 is widened as the engines widen it."""
    assert reference_token(LogicalType.STRING, "ab  ", 6, padded=True) == ("str", "ab")
    assert reference_token(LogicalType.STRING, "ab  ", 6) == ("str", "ab  ")
    assert reference_token(LogicalType.FLOAT, 0.1, 9, single_precision=True) == (
        "num",
        "0.100000001",
    )


def test_reference_refuses_to_guess():
    """Bytes as text, a fractional INTEGER, an unknown type: not checked."""
    assert reference_token(LogicalType.STRING, b"\x00", 6) is UNCHECKED
    assert reference_token(LogicalType.INTEGER, Decimal("1.5"), 6) is UNCHECKED
    assert reference_token(LogicalType.UNKNOWN, "x", 6) is UNCHECKED
    assert reference_token(LogicalType.STRING, None, 6) is None


# ------------------------------------------------------------- risk figures


def test_the_hidden_row_bound_is_the_exact_binomial_bound():
    """n sampled rows, none disagreeing: fewer than 1 - 0.01^(1/n) of rows can
    hide an error at 99% - and (1 - p)^n is then exactly 1%."""
    from parity.types import Verification

    v = Verification(10, 3, 1_000, 1_000)
    p = v.max_hidden_fraction()
    assert p == pytest.approx(0.004594582, rel=1e-6)
    assert (1 - p) ** 1_000 == pytest.approx(0.01, rel=1e-9)
    assert v.max_hidden_fraction(0.95) == pytest.approx(
        0.002991, rel=1e-3
    )  # rule of three


def test_a_full_comparison_has_no_hidden_rows_and_a_failure_has_no_bound():
    """Modulus 1 compared every row: the bound is exactly zero. A failed or
    errored check states no bound at all."""
    from parity.types import Verification

    assert Verification(1, 0, 50, 50).max_hidden_fraction() == 0.0
    assert Verification(5, 0, 9, 9, failures=["x"]).max_hidden_fraction() is None
    assert Verification(5, 0, 0, 0, error="down").max_hidden_fraction() is None


def test_every_run_reports_its_risk_figures():
    """The report and the JSON carry both figures, with the rows they cover."""
    import io

    from parity.cli import render_human, risk_figures

    a = FakeDialect(SyntheticTable(20_000, columns=COLS), side="A")
    b = FakeDialect(SyntheticTable(20_000, columns=COLS), side="B")
    result = diff(a, b, "t", "t", "id")
    r = risk_figures(result)
    assert r["checksum_collision_bound"] == 2.0**-60
    assert r["confidence"] == 0.99
    assert 0 < r["max_hidden_fraction"] < 0.01
    assert r["max_hidden_rows"] < 200 and r["rows"] == 20_000
    out = io.StringIO()
    render_human(result, out)
    assert "risk: with 99% confidence fewer than" in out.getvalue()
