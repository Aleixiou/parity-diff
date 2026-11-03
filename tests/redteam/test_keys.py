"""Red team: keys and the bisection walk.

Every test here plants a difference (or a deliberately *non*-difference) in
the key space and asserts what an honest parity checker must report. Confirmed
bugs are `xfail(strict=True)` with an `RT-B<n>` reason, so the suite stays
green while the bug exists and turns red the moment a fix lands - at which
point the marker comes off and the test becomes a regression guard.

Live engines are used where the behaviour is engine-specific; everything else
runs DuckDB <-> DuckDB, because the bugs below live in the engine and the
shared `Dialect` SQL, not in any one engine. All live objects are prefixed
`rt_key_` and dropped at the end of the module.
"""

from __future__ import annotations

import hashlib
import random
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import unquote, urlparse

import pytest
from conftest import MSSQL_URL, MYSQL_URL, PG_URL, duckdb_write, open_duckdb
from fakes import DictTable, FakeDialect, row_hash

from parity.dialects.base import get_dialect
from parity.dialects.snowflake_dialect import SnowflakeDialect
from parity.engine import bucket_bounds, diff
from parity.types import Column, LogicalType

PG_RT_SCHEMA = "rt_key_s"
MSSQL_RT_SCHEMA = "rt_key_s"
MYSQL_DB = urlparse(MYSQL_URL).path.lstrip("/") or "parity_test"


def _found(result) -> list[tuple]:
    """(key, kind) for every reported difference, in report order."""
    return [(d.key, d.kind) for d in result.diffs]


# ---------------------------------------------------------------- builders


def _duck(tmp_path, name: str, ddl: str, rows: list[tuple]) -> str:
    """A fresh DuckDB file holding table `t` with these rows; returns its path."""
    path = str(tmp_path / f"{name}.duckdb")
    con = duckdb_write(path)
    try:
        con.execute(f"create table t ({ddl})")
        # One multi-row INSERT per chunk: DuckDB's executemany costs ~8 ms a
        # row, which made the boundary sweep take a quarter of an hour.
        marks = "(" + ", ".join("?" * len(rows[0])) + ")" if rows else ""
        for i in range(0, len(rows), 500):
            chunk = rows[i : i + 500]
            con.execute(
                f"insert into t values {', '.join([marks] * len(chunk))}",
                [v for r in chunk for v in r],
            )
    finally:
        con.close()
    return path


def _duck_pair(tmp_path, ddl, rows_a, rows_b, ddl_b=None):
    """Open side A and side B DuckDB dialects over two freshly built files."""
    pa = _duck(tmp_path, "a", ddl, rows_a)
    pb = _duck(tmp_path, "b", ddl_b or ddl, rows_b)
    return open_duckdb(pa, side="A"), open_duckdb(pb, side="B")


def _duck_diff(tmp_path, ddl, rows_a, rows_b, key, ddl_b=None, **kw):
    """Diff two DuckDB tables built from literal rows."""
    a, b = _duck_pair(tmp_path, ddl, rows_a, rows_b, ddl_b)
    try:
        return diff(a, b, "main.t", "main.t", key, **kw)
    finally:
        a.close()
        b.close()


# ===========================================================================
# RT-B1 / RT-B2: a NULL inside a hashed key is neither refused nor distinct
# ===========================================================================
#
# An integer key with a NULL is refused (test_encoding.py). A *hashed* key -
# any text, uuid, decimal or composite key - renders its identity through
# `normalize`, which coalesces NULL to the sentinel `\N`. So `count(identity)`
# never sees a NULL, the NULL-key check never fires, and the key NULL becomes
# the two-byte string `\N` - indistinguishable from a row whose key really is
# the text `\N`. A literal `\N` is not exotic: it is the NULL marker of MySQL
# `SELECT ... INTO OUTFILE`, PostgreSQL `COPY ... TEXT` and Hive, and a
# migration that loads such a dump through a CSV path turns every NULL into
# exactly that string.


@pytest.mark.duckdb
@pytest.mark.xfail(
    strict=True,
    reason="RT-B1: NULL text key and literal '\\N' key are the same identity -> false match",
)
def test_rt_b1_null_text_key_is_not_the_string_backslash_n(tmp_path):
    rows_a = [(None, 1), ("x", 2)]
    rows_b = [("\\N", 1), ("x", 2)]  # the NULL became the dump marker
    result = _duck_diff(tmp_path, "k varchar, v int", rows_a, rows_b, "k")
    assert not result.identical, "NULL key vs the text '\\N' reported identical"


@pytest.mark.duckdb
@pytest.mark.xfail(
    strict=True,
    reason="RT-B1: NULL composite-key part equals literal '\\N' part -> false match",
)
def test_rt_b1_null_composite_key_part_is_not_backslash_n(tmp_path):
    rows_a = [(None, "x", 1), ("a", "y", 2)]
    rows_b = [("\\N", "x", 1), ("a", "y", 2)]
    result = _duck_diff(
        tmp_path, "k1 varchar, k2 varchar, v int", rows_a, rows_b, ["k1", "k2"]
    )
    assert not result.identical


@pytest.mark.duckdb
@pytest.mark.xfail(
    strict=True,
    reason="RT-B2: a NULL in a hashed (text/composite/bool) key is not refused",
)
@pytest.mark.parametrize(
    ("ddl", "row", "key"),
    [
        ("k varchar, v int", (None, 1), "k"),
        ("k boolean, v int", (None, 1), "k"),
        ("k uuid, v int", (None, 1), "k"),
        ("k1 int, k2 int, v int", (None, 1, 1), ["k1", "k2"]),
    ],
    ids=["text", "boolean", "uuid", "composite-int"],
)
def test_rt_b2_a_null_hashed_key_is_refused_like_a_null_integer_key(
    tmp_path, ddl, row, key
):
    """The integer-key contract (refuse, naming the side and NULL) must hold
    for every key shape. Today the row is reported as a key literally `\\N`."""
    with pytest.raises(ValueError, match="NULL"):
        _duck_diff(tmp_path, ddl, [row], [], key)


def test_control_null_integer_key_is_refused(tmp_path):
    """Negative control: the single-integer path does refuse."""
    pytest.importorskip("duckdb")
    with pytest.raises(ValueError, match="NULL"):
        _duck_diff(tmp_path, "k int, v int", [(None, 1)], [], "k")


# ===========================================================================
# RT-B3: the field separator inside key data merges identities
# ===========================================================================
#
# `key_identity` for a hashed key is `concat_ws(chr(31), parts...)`, and the
# checksum hashes `concat_ws(chr(31), key parts..., columns...)`. A 0x1f byte
# *inside* the data makes field boundaries ambiguous, and nothing escapes it.
# CLAUDE.md calls it "effectively never" present - but key columns are exactly
# where opaque, externally generated tokens live, and the result is silent.


@pytest.mark.duckdb
@pytest.mark.xfail(
    strict=True,
    reason="RT-B3: composite keys ('a\\x1fb','c') and ('a','b\\x1fc') share one identity",
)
def test_rt_b3_composite_keys_split_differently_are_different_rows(tmp_path):
    rows_a = [("a\x1fb", "c", 1)]
    rows_b = [("a", "b\x1fc", 1)]
    result = _duck_diff(
        tmp_path, "k1 varchar, k2 varchar, v int", rows_a, rows_b, ["k1", "k2"]
    )
    assert not result.identical


def _same_bucket_probe() -> str:
    """A key `s` such that `s` and `s + '\\x1fz'` share a top-level bucket.

    The range is fixed by two anchor rows plus the two probe keys, exactly as
    `diff` computes it, so a short search finds one (1 in 32 per try).
    """
    anchors = ["anchor-lo", "anchor-hi"]
    for j in range(100_000):
        s = f"p{j}"
        hs = [row_hash(x) for x in anchors] + [row_hash(s + "\x1fz"), row_hash(s)]
        lo, hi = min(hs), max(hs) + 1
        if (hs[2] - lo) * 32 // (hi - lo) == (hs[3] - lo) * 32 // (hi - lo):
            return s
    raise AssertionError("no probe found")  # pragma: no cover


@pytest.mark.duckdb
@pytest.mark.xfail(
    strict=True,
    reason="RT-B3: a 0x1f in a single text key slides into the content -> false match",
)
def test_rt_b3_single_text_key_with_separator_is_not_confused_with_content(tmp_path):
    """Side A has key `s\\x1fz` with v=`w`; side B has key `s` with v=`z\\x1fw`.
    Different keys, different values - but the hashed row text is
    `s\\x1fz\\x1fw` on both sides. When both keys land in one bucket, count and
    sum agree and the bucket is never opened."""
    s = _same_bucket_probe()
    anchors = [("anchor-lo", "same"), ("anchor-hi", "same")]
    rows_a = [*anchors, (s + "\x1fz", "w")]
    rows_b = [*anchors, (s, "z\x1fw")]
    result = _duck_diff(tmp_path, "k varchar, v varchar", rows_a, rows_b, "k")
    assert not result.identical


# ===========================================================================
# RT-B4: the additive bucket checksum is cancellable on purpose
# ===========================================================================
#
# A bucket is (count, sum of 60-bit row hashes).
#
# Threat model, re-verified: this needs an ADVERSARY, not bad luck.
#
# * By accident: for changes not chosen with the hash in mind, the bucket's
#   sum moves by a difference of MD5-derived 60-bit values, which is zero with
#   probability ~2^-60 per differing bucket. The structured, innocent changes a
#   migration actually makes - swaps, moves, same-content insert+delete,
#   duplicated content - cannot cancel systematically either, because the key
#   is folded into every row hash (`test_moves_swaps_and_same_content_reshuffles`
#   pins that). Not a practical accidental risk.
# * On purpose: cheap. MD5 has no secret, so anyone who can WRITE rows on one
#   side (and knows the hash fold, which is public in this repo) can pick new
#   values offline. Each row delta lies in (-2^60, 2^60), so eight of them sum
#   inside (-2^63, 2^63) and "sum == 0 mod 2^64" means "sum == 0". Wagner's
#   k-tree algorithm for k = 8 and 64 bits needs 2^16 candidates per row and
#   finds a set in ~2-4 s of pure Python (`_wagner_values` below, exercised by
#   `test_rt_b4_the_attack_is_cheap`). The eight rows must share one bucket at
#   every level, which adjacent integer keys do.
#
# So: false match, adversarial only. The tool is not tamper-evident against an
# insider rewriting rows on the new system, and must not be described as such;
# a keyed (HMAC-style) or wider non-linear checksum would be needed for that.

#: Found by the same attack as `_wagner_values`; pinned so the end-to-end test
#: needs no search. Row texts are f"{k}\x1f{value}" for keys 1..8.
WAGNER_VALUES = [
    "t6833",
    "t35724",
    "t50811",
    "t37945",
    "t13812",
    "t45820",
    "t37040",
    "t61588",
]


def test_rt_b4_precondition_the_eight_values_really_cancel():
    """The planted change is a genuine collision of the bucket sum, verified
    with the same hash fold the engines use."""
    orig = sum(row_hash(f"{k}\x1forig") for k in range(1, 9))
    forged = sum(
        row_hash(f"{k}\x1f{v}") for k, v in zip(range(1, 9), WAGNER_VALUES, strict=True)
    )
    assert orig == forged
    assert all(v != "orig" for v in WAGNER_VALUES)


@pytest.mark.duckdb
@pytest.mark.xfail(
    strict=True,
    reason="RT-B4: 8 crafted row changes cancel in the additive bucket sum -> identical",
)
def test_rt_b4_crafted_changes_cannot_cancel_in_one_bucket(tmp_path):
    rows_a = [(k, "orig") for k in range(1, 1001)]
    rows_b = [(k, WAGNER_VALUES[k - 1] if k <= 8 else "orig") for k in range(1, 1001)]
    result = _duck_diff(tmp_path, "k bigint, v varchar", rows_a, rows_b, "k")
    assert sorted(_found(result)) == [(k, "different") for k in range(1, 9)]


def _wagner_values(keys, orig: str = "orig", bits: int = 16) -> list[str] | None:
    """Wagner's k-tree attack on the bucket sum, for len(keys) == 8.

    Finds values v_i (spelled `t<j>`) with sum h(k_i, v_i) == sum h(k_i, orig),
    where h is the engines' 60-bit row-hash fold of `f"{k}\\x1f{v}"`. Works mod
    2^64, which is exact here (see the section comment). Pure Python, public
    inputs only - this is what "an adversary" costs.
    """
    m = 1 << 64
    lists = []
    for k in keys:
        base = row_hash(f"{k}\x1f{orig}")
        lists.append(
            [((row_hash(f"{k}\x1ft{j}") - base) % m, (f"t{j}",)) for j in range(1 << bits)]
        )
    level = 1
    while len(lists) > 1:
        mask = (1 << (bits * level)) - 1
        merged = []
        for left, right in zip(lists[0::2], lists[1::2], strict=True):
            by_low: dict[int, list] = {}
            for y, q in right:
                by_low.setdefault((-y) & mask, []).append((y, q))
            merged.append(
                [((x + y) % m, p + q) for x, p in left for y, q in by_low.get(x & mask, ())]
            )
        lists = merged
        level += 1
    return next((list(p) for s, p in lists[0] if s == 0), None)


def test_rt_b4_the_attack_is_cheap():
    """Not a bug in itself: pins the adversary's cost (seconds, no secrets).
    A different key set than the pinned one, so nothing here is precomputed."""
    keys = list(range(101, 109))
    forged = _wagner_values(keys)
    assert forged is not None
    assert sum(row_hash(f"{k}\x1forig") for k in keys) == sum(
        row_hash(f"{k}\x1f{v}") for k, v in zip(keys, forged, strict=True)
    )


@pytest.mark.postgres
@pytest.mark.duckdb
@pytest.mark.xfail(
    strict=True,
    reason="RT-B4: crafted cancelling changes also pass PostgreSQL <-> DuckDB",
)
def test_rt_b4_live_postgres_duckdb(pg_rt, tmp_path):
    rows_a = [(k, "orig") for k in range(1, 1001)]
    rows_b = [(k, WAGNER_VALUES[k - 1] if k <= 8 else "orig") for k in range(1, 1001)]
    t = _pg_table(pg_rt, "rt_key_wagner", "k bigint, v text", rows_a)
    p = _duck(tmp_path, "b", "k bigint, v varchar", rows_b)
    result = _live_diff(PG_URL, t, f"duckdb:///{p}", "main.t", "k")
    assert sorted(_found(result)) == [(k, "different") for k in range(1, 9)]


@pytest.mark.duckdb
def test_rt_b4_control_one_of_the_eight_changes_alone_is_found(tmp_path):
    """Negative control: without the other seven, the change is found."""
    rows_a = [(k, "orig") for k in range(1, 1001)]
    rows_b = [(1, WAGNER_VALUES[0]), *rows_a[1:]]
    result = _duck_diff(tmp_path, "k bigint, v varchar", rows_a, rows_b, "k")
    assert _found(result) == [(1, "different")]


# ===========================================================================
# RT-B5 / RT-B6: a lossy key rendering merges distinct keys silently
# ===========================================================================
#
# The float scale is documented for *values*. Applied to a *key*, it is not a
# precision caveat but an identity merge: two different rows on two sides are
# matched up as one, and their contents compared as if they were the same
# entity. The output's "comparing floats and decimals at 6 decimal places"
# line does not say keys are affected, and no warning names the key.


@pytest.mark.duckdb
@pytest.mark.xfail(
    strict=True,
    reason="RT-B5: DECIMAL key rounded to 6dp - keys 0.1234567 and 0.1234568 match",
)
def test_rt_b5_decimal_keys_differing_past_the_scale_are_different_rows(tmp_path):
    result = _duck_diff(
        tmp_path,
        "k decimal(20,10), v int",
        [(Decimal("0.1234567"), 1)],
        [(Decimal("0.1234568"), 1)],
        "k",
    )
    assert not result.identical or any(
        "key" in w and "scale" in w for w in result.warnings
    )


@pytest.mark.duckdb
@pytest.mark.xfail(
    strict=True,
    reason="RT-B5: DOUBLE key rounded to 6dp - distinct float keys match",
)
def test_rt_b5_double_keys_differing_past_the_scale_are_different_rows(tmp_path):
    result = _duck_diff(
        tmp_path, "k double, v int", [(1.0000001, 1)], [(1.0000004, 1)], "k"
    )
    assert not result.identical or any(
        "key" in w and "scale" in w for w in result.warnings
    )


@pytest.mark.duckdb
@pytest.mark.xfail(
    strict=True,
    reason="RT-B6: TIMESTAMP_NS key truncated to microseconds - distinct keys match",
)
def test_rt_b6_nanosecond_timestamp_keys_are_different_rows(tmp_path):
    for side, ns in (("a", 1), ("b", 2)):
        con = duckdb_write(str(tmp_path / f"{side}.duckdb"))
        con.execute("create table t (k timestamp_ns, v int)")
        con.execute(f"insert into t values ('2024-01-01 00:00:00.00000000{ns}', 1)")
        con.close()
    a = open_duckdb(str(tmp_path / "a.duckdb"), side="A")
    b = open_duckdb(str(tmp_path / "b.duckdb"), side="B")
    try:
        result = diff(a, b, "main.t", "main.t", "k")
    finally:
        a.close()
        b.close()
    # Either the keys stay distinct, or the run says it truncated them.
    assert not result.identical or any("nanosecond" in w for w in result.warnings)


@pytest.mark.duckdb
def test_rt_b5_control_decimal_keys_on_the_scale_are_exact(tmp_path):
    """Control: at or above the scale, decimal keys are matched exactly and a
    one-ulp key change is two rows."""
    result = _duck_diff(
        tmp_path,
        "k decimal(20,6), v int",
        [(Decimal("0.123456"), 1)],
        [(Decimal("0.123457"), 1)],
        "k",
    )
    assert _found(result) == [("0.123456", "only_in_a"), ("0.123457", "only_in_b")]


# ===========================================================================
# RT-B7 (offline): Snowflake's bucket division is floor(decimal '/')
# ===========================================================================
#
# SQL Server's `int_div` was rewritten to divide an exact multiple because
# decimal `/` could round q.9999996 up to q+1 and move a row into the next
# bucket. Snowflake's `int_div` is still `floor((n) / (d))` on NUMBER(38,0)
# operands. Snowflake gives a NUMBER(38,0)/NUMBER(38,0) quotient 6 decimal
# places and *rounds* it (`select 2/3` is 0.666667) - so any key whose quotient
# is within 5e-7 below an integer lands one bucket high, on that side, while
# `bucket_bounds` places it one bucket low. When BOTH sides are Snowflake (a
# warehouse-to-warehouse or prod-vs-dev comparison) the row is counted in a
# bucket whose Python range excludes it: a change to it is summed into a
# bucket that does differ, but the download/recursion of that bucket's range
# never sees the row, and the bucket whose range does contain it compares
# equal. Not verified live (Snowflake is offline for this campaign); one query
# settles it: `select floor(cast(100000000 as number(38,0)) /
# cast(100000001 as number(38,0)))` - 0 is exact, 1 confirms the bug.


def _snowflake_bucket(k: int, lo: int, hi: int, n: int) -> int:
    """`floor((k - lo) * n / (hi - lo))` as Snowflake evaluates NUMBER division:
    the quotient rounded half-up to scale 6, then floored."""
    q = (Decimal((k - lo) * n) / Decimal(hi - lo)).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    )
    return int(q // 1)


class _SnowflakeLikeFake(FakeDialect):
    """A fake whose checksum buckets follow Snowflake's division, nothing else."""

    def segment_checksums(self, table, key, columns, lo, hi, n_segments):
        self.queries += 1
        out: dict[int, list[int]] = {}
        for k in self.table.keys_in(lo, hi):
            acc = out.setdefault(_snowflake_bucket(k, lo, hi, n_segments), [0, 0])
            acc[0] += 1
            text = self.table.text(k, columns)
            acc[1] += row_hash(str(k) if not columns else f"{k}\x1f{text}")
        return {i: (c, s) for i, (c, s) in out.items()}


def test_rt_b7_snowflake_int_div_is_floor_of_decimal_division():
    """What the dialect emits today - the precondition of the model below."""
    d = SnowflakeDialect()
    assert d.int_div("n", "d") == "floor((n) / (d))"


@pytest.mark.xfail(
    strict=True,
    reason="RT-B7: Snowflake floor(n/d) rounds q.9999999 up a bucket (offline model)",
)
def test_rt_b7_snowflake_to_snowflake_change_on_a_near_boundary_key_is_found():
    span = 10**8 + 1  # keys 0 .. 10^8
    n = 32
    k_bad = (span - 1) * pow(n, -1, span) % span  # (k*n) % span == span - 1
    assert _snowflake_bucket(k_bad, 0, span, n) != (k_bad * n) // span
    cols = [Column("v", LogicalType.STRING, "varchar")]
    rows = {0: ("lo",), k_bad: ("same",), span - 1: ("hi",)}
    changed = {**rows, k_bad: ("CHANGED",)}
    a = _SnowflakeLikeFake(DictTable(cols, rows), side="A")
    b = _SnowflakeLikeFake(DictTable(cols, changed), side="B")
    result = diff(a, b, "t", "t", "id")
    assert _found(result) == [(k_bad, "different")]


# ===========================================================================
# RT-B8: key type mismatch is silent, and reads as total data loss
# ===========================================================================


@pytest.mark.duckdb
@pytest.mark.xfail(
    strict=True,
    reason="RT-B8: int key vs decimal(18,0) key - every row only_in_a/only_in_b, no warning",
)
def test_rt_b8_key_whose_type_differs_between_sides_is_named(tmp_path):
    """A bigint key migrated to `decimal(18,0)` (Oracle NUMBER, Snowflake NUMBER
    with a scale, a CSV round-trip). Both sides hash, rendering `42` against
    `42.000000`, so every row is reported missing on one side and present on
    the other. A *column* with this mismatch gets a "will very likely all
    differ" warning; the key gets nothing, and the run reads as if the new
    system lost the whole table."""
    rows = [(i, i * 10) for i in range(1, 6)]
    result = _duck_diff(
        tmp_path,
        "k bigint, v int",
        rows,
        rows,
        "k",
        ddl_b="k decimal(18,0), v int",
    )
    assert result.identical or any(
        "key" in w and ("side A" in w and "side B" in w) for w in result.warnings
    ), f"{len(result.diffs)} diffs, warnings={result.warnings}"


@pytest.mark.duckdb
def test_int_key_against_text_key_holding_the_same_digits_matches(tmp_path):
    """Control: bigint 42 against varchar '42' hashes both sides and matches,
    and '042' is still a different key."""
    rows_a = [(i, i) for i in range(1, 50)]
    rows_b = [(str(i), i) for i in range(1, 50)]
    clean = _duck_diff(
        tmp_path, "k bigint, v int", rows_a, rows_b, "k", ddl_b="k varchar, v int"
    )
    assert clean.identical
    rows_b[41] = ("042", 42)
    (tmp_path / "a.duckdb").unlink()
    (tmp_path / "b.duckdb").unlink()
    moved = _duck_diff(
        tmp_path, "k bigint, v int", rows_a, rows_b, "k", ddl_b="k varchar, v int"
    )
    assert sorted(_found(moved)) == [("042", "only_in_b"), ("42", "only_in_a")]


# ===========================================================================
# What the walk got right: structure of the key space (all pass)
# ===========================================================================

I64_MIN, I64_MAX = -(2**63), 2**63 - 1


def _boundary_keys(lo: int, hi: int, n: int) -> set[int]:
    """Every key at and either side of every top-level bucket boundary."""
    keys: set[int] = set()
    for i in range(n):
        b_lo, b_hi = bucket_bounds(i, lo, hi, n)
        keys |= {k for k in (b_lo - 1, b_lo, b_lo + 1, b_hi - 1) if lo <= k < hi}
    return keys


SHAPES = {
    "extremes": [I64_MIN, I64_MIN + 1, -1, 0, 1, I64_MAX - 1, I64_MAX],
    "all_negative": list(range(-5000, -4000, 7)),
    "straddle_zero": list(range(-300, 301, 3)),
    "two_far_clusters": list(range(1, 200)) + list(range(10**15, 10**15 + 200)),
    "min_only_cluster": [I64_MIN + i for i in range(60)],
    "max_only_cluster": [I64_MAX - i for i in range(60)],
}


@pytest.mark.duckdb
@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("bf", [2, 3, 7, 31, 32, 97, 250])
@pytest.mark.parametrize("threshold", [1, 3, 50])
def test_every_planted_boundary_difference_is_found(tmp_path, shape, bf, threshold):
    """Keys on every top-level bucket boundary for this factor, plus the shape,
    with a change, a delete and an insert planted on boundary keys. The walk
    must report exactly those - nothing skipped at an edge, nothing extra."""
    base = SHAPES[shape]
    lo, hi = min(base), max(base) + 1
    keys = sorted(set(base) | _boundary_keys(lo, hi, min(bf, hi - lo)))
    rnd = random.Random(f"{shape}-{bf}-{threshold}")
    a_rows = {k: f"v{k % 9}" for k in keys}
    b_rows = dict(a_rows)
    picks = rnd.sample(keys, min(len(keys), 9))
    expected = []
    for j, k in enumerate(picks):
        if j % 3 == 0:
            b_rows[k] = "changed"
            expected.append((k, "different"))
        elif j % 3 == 1:
            del b_rows[k]
            expected.append((k, "only_in_a"))
        else:
            del a_rows[k]
            expected.append((k, "only_in_b"))
    result = _duck_diff(
        tmp_path,
        "k bigint, v varchar",
        sorted(a_rows.items()),
        sorted(b_rows.items()),
        "k",
        bisection_factor=bf,
        threshold=threshold,
    )
    assert _found(result) == sorted(expected)


@pytest.mark.duckdb
@pytest.mark.parametrize("n_rows", [9, 10, 11])
@pytest.mark.parametrize("changed", ["first", "last", "none"])
def test_threshold_edges(tmp_path, n_rows, changed):
    """Exactly `threshold`, one under and one over: the bucket is either
    downloaded or recursed into, and either way the change is found once."""
    rows = [(k, "x") for k in range(1, n_rows + 1)]
    rows_b = list(rows)
    want = []
    if changed != "none":
        i = 0 if changed == "first" else -1
        rows_b[i] = (rows_b[i][0], "y")
        want = [(rows_b[i][0], "different")]
    result = _duck_diff(
        tmp_path,
        "k bigint, v varchar",
        rows,
        rows_b,
        "k",
        threshold=10,
        bisection_factor=2,
    )
    assert _found(result) == want
    if changed == "none":
        assert result.stats.rows_downloaded == 0


@pytest.mark.duckdb
@pytest.mark.parametrize(
    ("rows_a", "rows_b", "want"),
    [
        ([], [], []),
        ([(I64_MAX, "x")], [], [(I64_MAX, "only_in_a")]),
        ([], [(I64_MIN, "x")], [(I64_MIN, "only_in_b")]),
        (
            [(I64_MIN, "x")],
            [(I64_MAX, "x")],
            [(I64_MIN, "only_in_a"), (I64_MAX, "only_in_b")],
        ),
        ([(5, "x")], [(5, "y")], [(5, "different")]),
        ([(5, None)], [(5, "")], [(5, "different")]),
    ],
    ids=["both-empty", "b-empty", "a-empty", "opposite-ends", "one-row", "null-vs-empty"],
)
def test_degenerate_tables(tmp_path, rows_a, rows_b, want):
    result = _duck_diff(tmp_path, "k bigint, v varchar", rows_a, rows_b, "k")
    assert _found(result) == want


@pytest.mark.duckdb
@pytest.mark.parametrize("hashed", [False, True], ids=["int-key", "text-key"])
def test_moves_swaps_and_same_content_reshuffles(tmp_path, hashed):
    """Every content-preserving reshuffle of keys and values: the multiset of
    non-key values is identical on both sides, so only the key-folded hash can
    see them. Each must be found exactly."""

    def key(i):
        return f"key-{i}" if hashed else i

    ddl = ("k varchar" if hashed else "k bigint") + ", v varchar, w int"
    base = {key(i): (f"s{i % 3}", i % 2) for i in range(1, 400)}
    b = dict(base)
    # 1. a row moved to a fresh key (same content)
    b[key(1000)] = b.pop(key(10))
    # 2. two rows' contents swapped between keys
    b[key(20)], b[key(21)] = base[key(21)], base[key(20)]
    # 3. delete a row and insert identical content under a new key, same bucket
    b[key(31)] = b.pop(key(30))
    # 4. duplicate a row's content under a new key while deleting the original
    #    *and* a different row with identical content stays put
    b[key(1001)] = b.pop(key(40))
    want = sorted(
        [
            (key(10), "only_in_a"),
            (key(1000), "only_in_b"),
            (key(20), "different"),
            (key(21), "different"),
            (key(30), "only_in_a"),
            (key(31), "different"),
            (key(40), "only_in_a"),
            (key(1001), "only_in_b"),
        ],
        key=lambda t: (isinstance(t[0], str), t[0]),
    )
    result = _duck_diff(
        tmp_path,
        ddl,
        [(k, *v) for k, v in base.items()],
        [(k, *v) for k, v in b.items()],
        "k",
    )
    assert _found(result) == want


@pytest.mark.duckdb
@pytest.mark.parametrize(
    ("ka", "kb"),
    [("a", "A"), ("a", "a "), ("01", "1"), ("é", "é"), ("ab", "ab​")],
    ids=["case", "trailing-space", "leading-zero", "nfc-vs-nfd", "zero-width"],
)
def test_text_keys_that_look_alike_are_different_rows(tmp_path, ka, kb):
    result = _duck_diff(tmp_path, "k varchar, v int", [(ka, 1)], [(kb, 1)], "k")
    assert sorted(_found(result)) == sorted([(ka, "only_in_a"), (kb, "only_in_b")])


@pytest.mark.duckdb
def test_composite_parts_cannot_smear_without_the_separator(tmp_path):
    """Control for RT-B3: ('ab','c') vs ('a','bc') is separated correctly."""
    result = _duck_diff(
        tmp_path,
        "k1 varchar, k2 varchar, v int",
        [("ab", "c", 1)],
        [("a", "bc", 1)],
        ["k1", "k2"],
    )
    assert sorted(_found(result)) == [("a\x1fbc", "only_in_b"), ("ab\x1fc", "only_in_a")]


@pytest.mark.duckdb
@pytest.mark.parametrize("hashed", [False, True], ids=["int-key", "text-key"])
def test_duplicate_key_on_one_side_only_is_refused(tmp_path, hashed):
    k = "dup" if hashed else 7
    ddl = ("k varchar" if hashed else "k bigint") + ", v int"
    with pytest.raises(ValueError, match="not unique"):
        _duck_diff(tmp_path, ddl, [(k, 1)], [(k, 1), (k, 2)], "k")


# ===========================================================================
# Live engines
# ===========================================================================


def _mysql_con():
    import mysql.connector

    u = urlparse(MYSQL_URL)
    return mysql.connector.connect(
        host=u.hostname or "127.0.0.1",
        port=u.port or 3306,
        user=unquote(u.username) if u.username else None,
        password=unquote(u.password) if u.password else None,
        database=MYSQL_DB,
        autocommit=True,
    )


@pytest.fixture(scope="module")
def pg_rt(pg_url):
    """A PostgreSQL schema of our own, dropped at the end of the module."""
    import psycopg

    con = psycopg.connect(pg_url, autocommit=True)
    con.execute(f"drop schema if exists {PG_RT_SCHEMA} cascade")
    con.execute(f"create schema {PG_RT_SCHEMA}")
    yield con
    con.execute(f"drop schema if exists {PG_RT_SCHEMA} cascade")
    con.close()


def _pg_table(con, name: str, ddl: str, rows: list[tuple]) -> str:
    q = f"{PG_RT_SCHEMA}.{name}"
    con.execute(f"drop table if exists {q}")
    con.execute(f"create table {q} ({ddl})")
    if rows:
        marks = ", ".join(["%s"] * len(rows[0]))
        with con.cursor() as cur:
            cur.executemany(f"insert into {q} values ({marks})", rows)
    return q


@pytest.fixture(scope="module")
def mysql_rt(mysql_url):
    con = _mysql_con()
    made: list[str] = []
    yield con, made
    cur = con.cursor()
    for t in made:
        cur.execute(f"drop table if exists `{t}`")
    con.close()


def _mysql_table(rt, name: str, ddl: str, rows: list[tuple]) -> str:
    con, made = rt
    cur = con.cursor()
    cur.execute(f"drop table if exists `{name}`")
    cur.execute(f"create table `{name}` ({ddl})")
    made.append(name)
    if rows:
        marks = ", ".join(["%s"] * len(rows[0]))
        cur.executemany(f"insert into `{name}` values ({marks})", rows)
    return f"{MYSQL_DB}.{name}"


@pytest.fixture(scope="module")
def mssql_rt(mssql_url):
    import mssql_python

    from parity.dialects.mssql_dialect import parse_mssql_url

    odbc, _ = parse_mssql_url(mssql_url)
    con = mssql_python.connect(odbc, autocommit=True)
    cur = con.cursor()
    cur.execute(
        f"if schema_id('{MSSQL_RT_SCHEMA}') is null exec('create schema {MSSQL_RT_SCHEMA}')"
    )
    made: list[str] = []
    yield con, made
    for t in made:
        cur.execute(f"drop table if exists [{MSSQL_RT_SCHEMA}].[{t}]")
    cur.execute(
        f"if schema_id('{MSSQL_RT_SCHEMA}') is not null drop schema {MSSQL_RT_SCHEMA}"
    )
    con.close()


def _mssql_table(rt, name: str, ddl: str, rows: list[tuple]) -> str:
    con, made = rt
    cur = con.cursor()
    q = f"[{MSSQL_RT_SCHEMA}].[{name}]"
    cur.execute(f"drop table if exists {q}")
    cur.execute(f"create table {q} ({ddl})")
    made.append(name)
    if rows:
        marks = ", ".join(["?"] * len(rows[0]))
        cur.executemany(f"insert into {q} values ({marks})", rows)
    return f"{MSSQL_RT_SCHEMA}.{name}"


def _live_diff(url_a, t_a, url_b, t_b, key, **kw):
    a = get_dialect(url_a, side="A")
    b = get_dialect(url_b, side="B")
    try:
        return diff(a, b, t_a, t_b, key, **kw)
    finally:
        a.close()
        b.close()


# ---------------------------------------------------- RT-B1 across engines


@pytest.mark.postgres
@pytest.mark.duckdb
@pytest.mark.xfail(
    strict=True,
    reason="RT-B1: PostgreSQL NULL text key == DuckDB '\\N' key -> false match",
)
def test_rt_b1_live_postgres_null_key_vs_duckdb_backslash_n(pg_rt, tmp_path):
    t = _pg_table(pg_rt, "rt_key_nullk", "k text, v int", [(None, 1), ("x", 2)])
    p = _duck(tmp_path, "b", "k varchar, v int", [("\\N", 1), ("x", 2)])
    result = _live_diff(PG_URL, t, f"duckdb:///{p}", "main.t", "k")
    assert not result.identical


@pytest.mark.postgres
@pytest.mark.mysql
@pytest.mark.xfail(
    strict=True,
    reason="RT-B1: MySQL NULL composite part == PostgreSQL '\\N' -> false match",
)
def test_rt_b1_live_mysql_null_part_vs_postgres_backslash_n(pg_rt, mysql_rt):
    tm = _mysql_table(
        mysql_rt,
        "rt_key_nullc",
        "k1 varchar(10), k2 varchar(10), v int",
        [(None, "x", 1), ("a", "y", 2)],
    )
    tp = _pg_table(
        pg_rt, "rt_key_nullc", "k1 text, k2 text, v int", [("\\N", "x", 1), ("a", "y", 2)]
    )
    result = _live_diff(MYSQL_URL, tm, PG_URL, tp, ["k1", "k2"])
    assert not result.identical


@pytest.mark.mssql
@pytest.mark.postgres
@pytest.mark.xfail(
    strict=True,
    reason="RT-B1/B3: SQL Server NULL key and 0x1f composite keys false-match",
)
def test_rt_b1_b3_live_mssql_against_postgres(pg_rt, mssql_rt):
    ms = _mssql_table(
        mssql_rt,
        "rt_key_mix",
        "k1 nvarchar(20), k2 nvarchar(20), v int",
        [(None, "x", 1), ("a\x1fb", "c", 2), ("q", "r", 3)],
    )
    tp = _pg_table(
        pg_rt,
        "rt_key_mix",
        "k1 text, k2 text, v int",
        [("\\N", "x", 1), ("a", "b\x1fc", 2), ("q", "r", 3)],
    )
    result = _live_diff(MSSQL_URL, ms, PG_URL, tp, ["k1", "k2"])
    assert len(result.diffs) == 4, _found(result)


# ---------------------------------------------- RT-B9: MySQL collation keys


@pytest.mark.mysql
@pytest.mark.postgres
@pytest.mark.parametrize(
    ("collation", "keys"),
    [
        ("utf8mb4_bin", ["abc", "ABC"]),
        ("utf8mb4_bin", ["e", "é"]),
        ("utf8mb4_0900_bin", ["Ab", "aB"]),
    ],
    ids=["case", "accent", "0900-bin-case"],
)
def test_rt_b9_mysql_case_distinct_binary_keys_are_unique(
    pg_rt, mysql_rt, collation, keys
):
    """A `utf8mb4_bin` column (base62 short codes, case-sensitive natural keys)
    legitimately holds 'abc' and 'ABC'. `normalize` casts the key to the
    connection's `char`, which carries the default ci/ai collation, so
    `count(distinct)` merges them and the run refuses a perfectly unique key.
    SQL Server had exactly this and was fixed (`count_distinct_key` by
    SHA-256); MySQL was not."""
    rows = [(k, i) for i, k in enumerate(keys)]
    tm = _mysql_table(
        mysql_rt,
        f"rt_key_ci_{collation}_{len(keys[0])}{ord(keys[1][0])}",
        f"k varchar(10) collate {collation}, v int",
        rows,
    )
    tp = _pg_table(pg_rt, "rt_key_ci", "k text, v int", rows)
    clean = _live_diff(MYSQL_URL, tm, PG_URL, tp, "k")
    assert clean.identical


# ------------------------------------------------- the walk across engines


LIVE_KEYS = sorted(
    set(SHAPES["extremes"])
    | set(range(-40, 41, 3))
    | {10**15 + i for i in range(0, 60, 7)}
    | _boundary_keys(I64_MIN, I64_MAX + 1, 7)
)


def _plant(keys):
    a = {k: f"v{k % 5}" for k in keys}
    b = dict(a)
    picks = random.Random(11).sample(keys, 9)
    want = []
    for j, k in enumerate(picks):
        if j % 3 == 0:
            b[k] = "changed"
            want.append((k, "different"))
        elif j % 3 == 1:
            del b[k]
            want.append((k, "only_in_a"))
        else:
            del a[k]
            want.append((k, "only_in_b"))
    return sorted(a.items()), sorted(b.items()), sorted(want)


@pytest.mark.postgres
@pytest.mark.mysql
@pytest.mark.parametrize("bf", [2, 7, 32])
def test_live_walk_postgres_mysql_boundaries_and_extremes(pg_rt, mysql_rt, bf):
    rows_a, rows_b, want = _plant(LIVE_KEYS)
    tp = _pg_table(pg_rt, "rt_key_walk", "k bigint, v text", rows_a)
    tm = _mysql_table(mysql_rt, "rt_key_walk", "k bigint, v varchar(20)", rows_b)
    result = _live_diff(PG_URL, tp, MYSQL_URL, tm, "k", bisection_factor=bf, threshold=2)
    assert _found(result) == want


@pytest.mark.postgres
@pytest.mark.mssql
@pytest.mark.parametrize("bf", [3, 32])
def test_live_walk_mssql_postgres_boundaries_and_extremes(pg_rt, mssql_rt, bf):
    rows_a, rows_b, want = _plant(LIVE_KEYS)
    ms = _mssql_table(mssql_rt, "rt_key_walk", "k bigint, v nvarchar(20)", rows_a)
    tp = _pg_table(pg_rt, "rt_key_walk2", "k bigint, v text", rows_b)
    result = _live_diff(MSSQL_URL, ms, PG_URL, tp, "k", bisection_factor=bf, threshold=2)
    assert _found(result) == want


@pytest.mark.postgres
@pytest.mark.mysql
@pytest.mark.parametrize("dialect", ["postgres", "mysql"])
def test_live_bucket_expression_matches_bucket_bounds_at_every_boundary(
    pg_rt, mysql_rt, dialect
):
    """The SQL Server boundary check, repeated for PostgreSQL and MySQL: every
    key at and beside every bucket edge of random ranges up to 2^64 wide must
    get exactly the bucket Python assigns it, including keys whose quotient is
    a hair below an integer (the rounding-division trap)."""
    rnd = random.Random(23)
    cases = [(I64_MIN, I64_MAX + 1, 32), (0, 10**8 + 1, 32)]
    for _ in range(25):
        lo = rnd.randint(I64_MIN, I64_MAX - 2)
        hi = rnd.randint(lo + 2, min(I64_MAX + 1, lo + rnd.choice([10**3, 10**9, 2**64])))
        cases.append((lo, hi, rnd.choice([2, 3, 7, 32, 97])))
    keys: set[int] = set()
    for lo, hi, n in cases:
        keys |= _boundary_keys(lo, hi, n)
    span = 10**8 + 1
    keys.add((span - 1) * pow(32, -1, span) % span)  # quotient q.99999999
    rows = [(k,) for k in sorted(keys)]
    if dialect == "postgres":
        t = _pg_table(pg_rt, "rt_key_bkeys", "k bigint", rows)
        d = get_dialect(PG_URL, side="A")
    else:
        t = _mysql_table(mysql_rt, "rt_key_bkeys", "k bigint", rows)
        d = get_dialect(MYSQL_URL, side="A")
    try:
        for lo, hi, n in cases:
            k = d.quote("k")
            offset = f"({d.wide_int(k)} - ({d.int_literal(lo)}))"
            bucket = d.int_div(f"{offset} * {n}", f"({d.int_literal(hi - lo)})")
            got = d.query(
                f"select k, {bucket} from {d.qualify(t)} "
                f"where k >= {lo} and k <= {hi - 1}"
            )
            for key, b in got:
                assert int(b) == (int(key) - lo) * n // (hi - lo), (lo, hi, n, key)
    finally:
        d.close()


@pytest.mark.postgres
@pytest.mark.duckdb
def test_live_hashed_reshuffles_postgres_duckdb(pg_rt, tmp_path):
    """The 0.2.1 precedent family across engines with a uuid-like text key:
    same-content insert+delete, value swap, and a move, all in one table."""
    keys = [hashlib.md5(str(i).encode()).hexdigest() for i in range(300)]
    base = [(k, "same") for k in keys]
    b = dict(base)
    b[keys[0]], b[keys[1]] = "a", "b"  # contents now differ
    b["moved-" + keys[2]] = b.pop(keys[2])
    b["new"] = b.pop(keys[3])
    tp = _pg_table(pg_rt, "rt_key_shuf", "k text, v text", base)
    p = _duck(tmp_path, "b", "k varchar, v varchar", sorted(b.items()))
    result = _live_diff(PG_URL, tp, f"duckdb:///{p}", "main.t", "k")
    want = sorted(
        [
            (keys[0], "different"),
            (keys[1], "different"),
            (keys[2], "only_in_a"),
            ("moved-" + keys[2], "only_in_b"),
            (keys[3], "only_in_a"),
            ("new", "only_in_b"),
        ]
    )
    assert _found(result) == want


@pytest.mark.mssql
@pytest.mark.postgres
def test_live_mssql_ci_keys_against_postgres(pg_rt, mssql_rt):
    """Control for RT-B9: SQL Server's CI collation keys stay distinct."""
    rows = [("abc", 1), ("ABC", 2), ("abc ", 3)]
    ms = _mssql_table(mssql_rt, "rt_key_ci", "k nvarchar(10), v int", rows)
    tp = _pg_table(pg_rt, "rt_key_ci2", "k text, v int", [*rows[:2], ("abc ", 30)])
    result = _live_diff(MSSQL_URL, ms, PG_URL, tp, "k")
    assert _found(result) == [("abc ", "different")]



# ===========================================================================
# Wave 2: findings and controls added after the resume
# ===========================================================================

# ------------------------------------------- RT-B8 (variant): key timezone


@pytest.mark.postgres
@pytest.mark.duckdb
@pytest.mark.xfail(
    strict=True,
    reason="RT-B8: timestamptz key vs naive local-time key - all rows misreported, no warning",
)
def test_rt_b8_timezone_aware_key_against_naive_key_is_named(pg_rt, tmp_path):
    """`timestamptz` -> `timestamp` holding local wall-clock is one of the
    commonest migration changes. For a *column* the engine warns
    ("timezone-aware on side A but not on side B"); `_select_columns` skips
    key columns, so a key with the same change gets nothing and the run reads
    as every row deleted and re-inserted."""
    t = _pg_table(
        pg_rt,
        "rt_key_tzk",
        "k timestamptz, v int",
        [("2024-06-15 12:00:00+00", 1), ("2024-01-15 12:00:00+00", 2)],
    )
    p = _duck(
        tmp_path,
        "b",
        "k timestamp, v int",
        [("2024-06-15 14:00:00", 1), ("2024-01-15 13:00:00", 2)],  # Europe/Zurich
    )
    result = _live_diff(PG_URL, t, f"duckdb:///{p}", "main.t", "k")
    assert result.identical or any("timezone" in w for w in result.warnings), (
        _found(result),
        result.warnings,
    )


# -------------------------- RT-B10: PostgreSQL nondeterministic collations


@pytest.mark.postgres
@pytest.mark.xfail(
    strict=True,
    reason=(
        "RT-B10: PG nondeterministic-collation key: count(distinct) merges "
        "'abc'/'ABC' -> false 'not unique'"
    ),
)
def test_rt_b10_postgres_nondeterministic_collation_key_is_unique(pg_rt):
    """PostgreSQL 12+ ICU nondeterministic collations (case-insensitive natural
    keys) make `count(distinct k::text)` compare under the collation, because
    the cast keeps it. 'abc' and 'ABC' are two rows - the table holds both,
    md5 and `fetch_range` keep them apart - but the uniqueness check counts
    one and refuses the run. SQL Server fixed exactly this with SHA-256
    (`count_distinct_key`); PostgreSQL and MySQL (RT-B9) did not."""
    pg_rt.execute(
        f"create collation if not exists {PG_RT_SCHEMA}.rt_key_ci "
        "(provider = icu, locale = 'und-u-ks-level2', deterministic = false)"
    )
    ta = _pg_table(
        pg_rt,
        "rt_key_ndc",
        f"k text collate {PG_RT_SCHEMA}.rt_key_ci, v int",
        [("abc", 1), ("ABC", 2)],
    )
    tb = _pg_table(pg_rt, "rt_key_ndc2", "k text, v int", [("abc", 1), ("ABC", 3)])
    result = _live_diff(PG_URL, ta, PG_URL, tb, "k")
    assert _found(result) == [("ABC", "different")]


# ---------------------------------- RT-B11: PostgreSQL +/-infinity as NULL


@pytest.mark.postgres
@pytest.mark.xfail(
    strict=True,
    reason=(
        "RT-B11: PG to_char(+/-infinity) is NULL -> renders '\\N': "
        "infinity == -infinity == NULL"
    ),
)
def test_rt_b11_postgres_infinity_values_are_not_null(pg_rt):
    """`to_char('infinity'::timestamp, ...)` returns SQL NULL, which the
    coalesce turns into the NULL sentinel. So a date/timestamp/timestamptz
    holding +infinity, -infinity or NULL renders identically: a migration that
    turned the "open-ended" sentinel `infinity` (the idiomatic PostgreSQL
    valid_to / expires_at) into NULL, or flipped its sign, compares as a
    match. Verified PG<->PG and PG<->DuckDB (DuckDB NULL)."""
    ddl = "k int, ts timestamp, d date, tz timestamptz"
    a = [(1, "infinity", "infinity", "infinity"), (2, "-infinity", "2024-01-01", None)]
    b = [(1, None, None, None), (2, "infinity", "2024-01-01", "-infinity")]
    ta = _pg_table(pg_rt, "rt_key_inf_a", ddl, a)
    tb = _pg_table(pg_rt, "rt_key_inf_b", ddl, b)
    result = _live_diff(PG_URL, ta, PG_URL, tb, "k")
    assert [k for k, _ in _found(result)] == [1, 2]


@pytest.mark.postgres
@pytest.mark.duckdb
@pytest.mark.xfail(
    strict=True,
    reason="RT-B11: PG infinity date value == DuckDB NULL -> false match",
)
def test_rt_b11_postgres_infinity_against_duckdb_null(pg_rt, tmp_path):
    ta = _pg_table(
        pg_rt, "rt_key_inf_c", "k int, d date", [(1, "infinity"), (2, "2024-01-01")]
    )
    p = _duck(tmp_path, "b", "k int, d date", [(1, None), (2, "2024-01-01")])
    result = _live_diff(PG_URL, ta, f"duckdb:///{p}", "main.t", "k")
    assert _found(result) == [(1, "different")]


@pytest.mark.postgres
@pytest.mark.xfail(
    strict=True,
    reason=(
        "RT-B11: PG timestamp keys infinity and -infinity share one identity "
        "-> false 'not unique'"
    ),
)
def test_rt_b11_postgres_infinity_keys_are_distinct(pg_rt):
    rows = [("infinity", 1), ("-infinity", 2), ("2024-01-01", 3)]
    ta = _pg_table(pg_rt, "rt_key_infk_a", "k timestamp, v int", rows)
    tb = _pg_table(pg_rt, "rt_key_infk_b", "k timestamp, v int", rows)
    assert _live_diff(PG_URL, ta, PG_URL, tb, "k").identical


@pytest.mark.duckdb
def test_rt_b11_control_duckdb_renders_infinity(tmp_path):
    """Control: DuckDB's strftime keeps 'infinity' apart from NULL."""
    result = _duck_diff(tmp_path, "k int, d date", [(1, "infinity")], [(1, None)], "k")
    assert _found(result) == [(1, "different")]


# ------------------------ RT-B12: MySQL binary strings that are not UTF-8


@pytest.mark.mysql
@pytest.mark.xfail(
    strict=True,
    reason=(
        "RT-B12: MySQL varbinary key with non-UTF-8 bytes renders NULL "
        "-> distinct keys match"
    ),
)
def test_rt_b12_mysql_binary_keys_are_not_merged(mysql_rt):
    """`cast(k as char)` of bytes that are not valid utf8mb4 is SQL NULL (with
    only a warning), so it becomes the NULL sentinel: 0xFF01 on side A and
    0xFE02 on side B are both the key '\\N', matched up, and found equal."""
    ta = _mysql_table(
        mysql_rt,
        "rt_key_bin_a",
        "k varbinary(16), v int",
        [(b"abc", 1), (b"\xff\x01", 2)],
    )
    tb = _mysql_table(
        mysql_rt,
        "rt_key_bin_b",
        "k varbinary(16), v int",
        [(b"abc", 1), (b"\xfe\x02", 2)],
    )
    result = _live_diff(MYSQL_URL, ta, MYSQL_URL, tb, "k")
    assert not result.identical


@pytest.mark.mysql
@pytest.mark.xfail(
    strict=True,
    reason="RT-B12: MySQL binary(16) UUID keys all render '\\N' -> false 'not unique'",
)
def test_rt_b12_mysql_binary16_uuid_keys_are_unique(mysql_rt):
    """`binary(16)` is *the* MySQL idiom for UUID primary keys; random UUID
    bytes are almost never valid UTF-8, so the whole table collapses to a
    couple of identities and is refused as non-unique."""
    rnd = random.Random(5)
    rows = [(rnd.getrandbits(128).to_bytes(16, "big"), "same") for _ in range(50)]
    ta = _mysql_table(mysql_rt, "rt_key_bin16_a", "k binary(16), v varchar(8)", rows)
    tb = _mysql_table(mysql_rt, "rt_key_bin16_b", "k binary(16), v varchar(8)", rows)
    assert _live_diff(MYSQL_URL, ta, MYSQL_URL, tb, "k").identical


@pytest.mark.mysql
@pytest.mark.xfail(
    strict=True,
    reason=(
        "RT-B12: MySQL varbinary value change between non-UTF-8 bytes "
        "(or to NULL) is invisible"
    ),
)
def test_rt_b12_mysql_binary_value_changes_are_found(mysql_rt):
    """Same mechanism on a compared column. The run does warn "unmapped types,
    compared as raw text (may differ ...)", which promises false alarms, not
    false matches."""
    ta = _mysql_table(
        mysql_rt,
        "rt_key_binv_a",
        "k int, v varbinary(16)",
        [(1, b"\xff\x01"), (2, b"\xff")],
    )
    tb = _mysql_table(
        mysql_rt,
        "rt_key_binv_b",
        "k int, v varbinary(16)",
        [(1, b"\xfe\x02"), (2, None)],
    )
    result = _live_diff(MYSQL_URL, ta, MYSQL_URL, tb, "k")
    assert [k for k, _ in _found(result)] == [1, 2]


BINARY_ROWS_A = [(b"\xff\x01", "x"), (b"\x80" * 16, "x")]
BINARY_ROWS_B = [(b"\xfe\x02", "x"), (b"\x80" * 16, "y")]


@pytest.mark.postgres
def test_rt_b12_control_postgres_bytea_keys_are_exact(pg_rt):
    """Control: PostgreSQL renders every byte of a bytea key (hex)."""
    ta = _pg_table(pg_rt, "rt_key_bya", "k bytea, v text", BINARY_ROWS_A)
    tb = _pg_table(pg_rt, "rt_key_byb", "k bytea, v text", BINARY_ROWS_B)
    result = _live_diff(PG_URL, ta, PG_URL, tb, "k")
    assert sorted(kind for _, kind in _found(result)) == [
        "different",
        "only_in_a",
        "only_in_b",
    ]


@pytest.mark.duckdb
def test_rt_b12_control_duckdb_blob_keys_are_exact(tmp_path):
    """Control: DuckDB renders every byte of a blob key (escaped)."""
    result = _duck_diff(
        tmp_path, "k blob, v varchar", BINARY_ROWS_A, BINARY_ROWS_B, "k"
    )
    assert sorted(kind for _, kind in _found(result)) == [
        "different",
        "only_in_a",
        "only_in_b",
    ]


# ------------------------------------- RT-B13: DuckDB HUGEINT key overflow


@pytest.mark.duckdb
@pytest.mark.xfail(
    strict=True,
    raises=OverflowError,
    reason=(
        "RT-B13: DuckDB HUGEINT key wider than ~2^122 overflows INT128 - "
        "raw crash, no side named"
    ),
)
@pytest.mark.parametrize(
    "keys",
    [[0, 2**123], [-(2**126), 2**126]],
    ids=["times-n-overflows", "offset-overflows"],
)
def test_rt_b13_hugeint_key_range_does_not_crash(tmp_path, keys):
    """`hugeint` is mapped to INTEGER and bisected directly, but `wide_int` is
    `cast(k as hugeint)` - no wider than the column - so `(k - lo) * n`
    overflows INT128 once the span passes 2^127 / n. It raises (not a wrong
    answer), but as a bare duckdb.OutOfRangeException naming neither side nor
    table. A span of exactly 2^128 would also make the span literal a DOUBLE."""
    import duckdb

    rows_a = [(k, "x") for k in keys]
    rows_b = [(keys[0], "x"), (keys[1], "y")]
    try:
        result = _duck_diff(tmp_path, "k hugeint, v varchar", rows_a, rows_b, "k")
    except duckdb.OutOfRangeException as exc:
        # The xfail is for *this* crash only; any other error fails the test.
        if "Overflow" not in str(exc):
            raise
        raise OverflowError(str(exc)) from exc
    assert _found(result) == [(keys[1], "different")]


@pytest.mark.duckdb
@pytest.mark.parametrize(
    ("ddl", "keys"),
    [
        ("k hugeint, v varchar", [2**100, 2**100 + 1, 2**101]),
        ("k ubigint, v varchar", [0, 2**63 - 1, 2**63, 2**64 - 1]),
    ],
    ids=["hugeint-2^100", "ubigint-full-range"],
)
def test_wide_integer_keys_within_range_are_exact(tmp_path, ddl, keys):
    """Control: integer keys beyond int64 are walked exactly while the span
    stays inside INT128."""
    rows_a = [(k, "x") for k in keys]
    rows_b = [(k, "y" if k == keys[1] else "x") for k in keys if k != keys[-1]]
    result = _duck_diff(
        tmp_path, ddl, rows_a, rows_b, "k", threshold=1, bisection_factor=2
    )
    assert _found(result) == [(keys[1], "different"), (keys[-1], "only_in_a")]


@pytest.mark.mysql
@pytest.mark.duckdb
def test_live_mysql_unsigned_bigint_against_duckdb_ubigint(mysql_rt, tmp_path):
    """Control: keys past 2^63 (MySQL BIGINT UNSIGNED) bucket identically to
    DuckDB UBIGINT through MySQL's decimal(65,0) widening."""
    keys = [0, 2**63 - 1, 2**63, 2**64 - 1]
    tm = _mysql_table(
        mysql_rt,
        "rt_key_ubig",
        "k bigint unsigned, v varchar(5)",
        [(k, "x") for k in keys],
    )
    p = _duck(
        tmp_path, "b", "k ubigint, v varchar", [(0, "x"), (2**63, "y"), (2**64 - 1, "x")]
    )
    result = _live_diff(
        MYSQL_URL, tm, f"duckdb:///{p}", "main.t", "k", threshold=1, bisection_factor=2
    )
    assert _found(result) == [(2**63 - 1, "only_in_a"), (2**63, "different")]


# ------------------------------------------------------------ SQL Server


@pytest.mark.mssql
def test_live_mssql_bucket_expression_matches_bucket_bounds(mssql_rt):
    """The boundary check from the PostgreSQL/MySQL test, on SQL Server's
    exact-multiple `int_div`: every key at and beside every bucket edge of
    random ranges up to 2^64 wide gets exactly Python's bucket, including the
    quotient-q.99999999 key that rounding decimal `/` used to move."""
    rnd = random.Random(29)
    cases = [(I64_MIN, I64_MAX + 1, 32), (0, 10**8 + 1, 32)]
    for _ in range(25):
        lo = rnd.randint(I64_MIN, I64_MAX - 2)
        hi = rnd.randint(
            lo + 2, min(I64_MAX + 1, lo + rnd.choice([10**3, 10**9, 2**64]))
        )
        cases.append((lo, hi, rnd.choice([2, 3, 7, 32, 97])))
    keys: set[int] = set()
    for lo, hi, n in cases:
        keys |= _boundary_keys(lo, hi, n)
    span = 10**8 + 1
    keys.add((span - 1) * pow(32, -1, span) % span)
    t = _mssql_table(mssql_rt, "rt_key_bkeys", "k bigint", [(k,) for k in sorted(keys)])
    d = get_dialect(MSSQL_URL, side="A")
    try:
        for lo, hi, n in cases:
            k = d.quote("k")
            offset = f"({d.wide_int(k)} - ({d.int_literal(lo)}))"
            bucket = d.int_div(f"{offset} * {n}", f"({d.int_literal(hi - lo)})")
            got = d.query(
                f"select k, {bucket} from {d.qualify(t)} "
                f"where k >= {lo} and k <= {hi - 1}"
            )
            for key, b in got:
                assert int(b) == (int(key) - lo) * n // (hi - lo), (lo, hi, n, key)
    finally:
        d.close()


@pytest.mark.mssql
@pytest.mark.postgres
@pytest.mark.parametrize(
    ("ms_ddl", "pg_ddl", "rows_ms", "rows_pg", "want"),
    [
        (
            "k varbinary(16), v int",
            "k bytea, v int",
            [(b"\xff\x01", 1)],
            [(b"\xff\x01", 1)],
            None,  # 0x... against \x...: cross-engine binary keys never match
        ),
        (
            "k char(5), v int",
            "k char(5), v int",
            [("abc", 1), ("de", 2)],
            [("abc", 1), ("de", 3)],
            [("de", "different")],
        ),
        (
            "k nvarchar(10) collate Latin1_General_CS_AS, v int",
            "k text, v int",
            [("abc", 1), ("ABC", 2)],
            [("abc", 1), ("ABC", 20)],
            [("ABC", "different")],
        ),
        (
            "k uniqueidentifier, v int",
            "k uuid, v int",
            [("6F9619FF-8B86-D011-B42D-00C04FC964FF", 1)],
            [("6f9619ff-8b86-d011-b42d-00c04fc964ff", 2)],
            [("6f9619ff-8b86-d011-b42d-00c04fc964ff", "different")],
        ),
    ],
    ids=["varbinary-vs-bytea", "char-pad", "cs-collation", "uniqueidentifier"],
)
def test_live_mssql_key_shapes_against_postgres(
    pg_rt, mssql_rt, ms_ddl, pg_ddl, rows_ms, rows_pg, want
):
    """Controls: SQL Server key shapes against PostgreSQL. char(n) padding,
    a case-sensitive collation and uniqueidentifier's upper-case rendering all
    line up; binary keys do not (different hex spellings), recorded here as
    current behaviour - a false alarm on every row, never a false match."""
    name = "rt_key_shape_" + ms_ddl.split()[1].split("(")[0].rstrip(",")
    ms = _mssql_table(mssql_rt, name, ms_ddl, rows_ms)
    tp = _pg_table(pg_rt, name, pg_ddl, rows_pg)
    result = _live_diff(MSSQL_URL, ms, PG_URL, tp, "k")
    if want is None:
        assert sorted(kind for _, kind in _found(result)) == ["only_in_a", "only_in_b"]
    else:
        assert _found(result) == want


@pytest.mark.mssql
@pytest.mark.xfail(
    strict=True,
    reason="RT-B1: SQL Server NULL text key == SQL Server '\\N' key -> false match",
)
def test_rt_b1_live_mssql_to_mssql(mssql_rt):
    ta = _mssql_table(
        mssql_rt, "rt_key_nk_a", "k nvarchar(10), v int", [(None, 1), ("x", 2)]
    )
    tb = _mssql_table(
        mssql_rt, "rt_key_nk_b", "k nvarchar(10), v int", [("\\N", 1), ("x", 2)]
    )
    assert not _live_diff(MSSQL_URL, ta, MSSQL_URL, tb, "k").identical


@pytest.mark.mssql
@pytest.mark.xfail(
    strict=True,
    reason="RT-B3: SQL Server 0x1f inside composite key parts merges identities",
)
def test_rt_b3_live_mssql_to_mssql(mssql_rt):
    ddl = "k1 nvarchar(10), k2 nvarchar(10), v int"
    ta = _mssql_table(mssql_rt, "rt_key_sep_a", ddl, [("a\x1fb", "c", 1)])
    tb = _mssql_table(mssql_rt, "rt_key_sep_b", ddl, [("a", "b\x1fc", 1)])
    assert not _live_diff(MSSQL_URL, ta, MSSQL_URL, tb, ["k1", "k2"]).identical
