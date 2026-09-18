"""Differential fuzzing of the cross-engine encoding contract.

`test_encoding.py` pins the encoding with a fixed table of hand-chosen cases -
the seventeen values CLAUDE.md section 4 was verified against. This file attacks
the same contract from the other side: it *generates* hundreds of values, many
of them deliberately hostile (the field separator byte itself, the NULL
sentinel spelled as real data, emoji, combining marks, right-to-left text,
bigint extremes, microsecond timestamps), inserts the **same Python value** into
both engines, and asserts they render byte-identical canonical text and the same
60-bit row hash.

Because the value inserted into each side is identical, any disagreement is a
pure *encoding* difference - exactly the class of bug that makes a migration
look clean when a NULL quietly became an empty string, or a timestamp lost its
microseconds on one engine only. A fixed case list can only catch the
differences someone already thought to write down; this catches the ones nobody
did.

Values are inserted through the drivers' parameter binding, never as SQL
literals, so an arbitrary string cannot escape into the statement - the fuzz
input is data, not code. The run is seeded, so a failure reproduces exactly.

Skips, never fails, when an engine is unreachable (see conftest).
"""

from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal

import pytest
from conftest import open_duckdb, open_pg

from parity.dialects.base import Dialect
from parity.engine import diff
from parity.types import Column

# A schema and a DuckDB file of this module's own, so nothing here collides with
# the fixtures in test_encoding.py (DuckDB permits a single writer per file).
FUZZ_SCHEMA = "parity_fuzz"
FUZZ_TABLE = "fuzz_values"
SEED = 20240906
N_RANDOM = 250

# id bigint, then one column per fuzzable logical type. The tuple is
# (name, postgres type, duckdb type).
COLUMNS: list[tuple[str, str, str]] = [
    ("id", "bigint", "bigint"),
    ("s", "text", "varchar"),
    ("i", "bigint", "bigint"),
    ("d", "decimal(20,6)", "decimal(20,6)"),
    ("b", "boolean", "boolean"),
    ("dd", "date", "date"),
    ("ts", "timestamp", "timestamp"),
]
VALUE_COLUMNS = [name for name, *_ in COLUMNS if name != "id"]

# Characters excluded from generated strings for reasons unrelated to parity:
# PostgreSQL's `text` type cannot store a NUL byte at all, and lone surrogates
# have no UTF-8 encoding. Everything else - including the 0x1f field separator
# and the two bytes of the `\N` sentinel - is fair game: a single column's
# canonical text never involves the separator, so both engines must still
# render these identically, and if they don't that is the bug.
_FORBIDDEN = {"\x00"}


def _rand_string(rng: random.Random) -> str:
    """A random string drawn from a deliberately hostile alphabet."""
    palette = (
        "abcABC012 "
        "héllo wörld"  # accented latin
        "日本語한국어"  # CJK
        "\U0001f389\U0001f600"  # emoji (astral plane)
        "éñ"  # combining marks
        "‏‮"  # RTL / override controls
        "\x1f"  # the field separator, as real data
        "\t\r\n"  # whitespace controls
    )
    length = rng.randint(0, 24)
    chars = [rng.choice(palette) for _ in range(length)]
    s = "".join(c for c in chars if c not in _FORBIDDEN)
    # Encoding round-trips as UTF-8 downstream; drop anything that cannot.
    return s.encode("utf-8", "ignore").decode("utf-8")


def _rand_decimal(rng: random.Random) -> Decimal:
    """A decimal with at most 6 places, so no rounding happens at insert time.

    A value with more than 6 places would be rounded into decimal(20,6) by the
    INSERT, and if the two engines rounded it differently the *stored* values
    would differ - a value difference masquerading as an encoding difference.
    Six places keeps the inserted value exact on both sides.
    """
    unscaled = rng.randint(-(10**12), 10**12)
    scale = rng.randint(0, 6)
    return (Decimal(unscaled) / (Decimal(10) ** scale)).quantize(Decimal("0.000001"))


def _rand_timestamp(rng: random.Random) -> dt.datetime:
    """A timestamp somewhere in a wide range, to microsecond precision."""
    base = dt.datetime(1000, 1, 1)
    return base + dt.timedelta(
        days=rng.randint(0, 3_000_000),
        seconds=rng.randint(0, 86_399),
        microseconds=rng.randint(0, 999_999),
    )


def _adversarial_rows() -> list[tuple]:
    """Hand-picked hostile rows, prepended so they are always exercised.

    Each is a `(s, i, d, b, dd, ts)` tuple; `None` means SQL NULL, which must
    render as the sentinel rather than poison the row.
    """
    return [
        ("", 0, Decimal("0.000000"), False, dt.date(1970, 1, 1), dt.datetime(2024, 1, 1)),
        (
            "\x1f",
            1,
            Decimal("1.500000"),
            True,
            dt.date(2024, 2, 29),
            dt.datetime(2024, 2, 29, 13, 4, 5, 123456),
        ),
        # The sentinel spelled as genuine string data - both engines must still
        # agree on how they render it, whatever the diff logic later makes of it.
        ("\\N", -1, Decimal("-0.125000"), None, None, None),
        (
            'O\'Brien "x"',
            9223372036854775807,
            Decimal("123456789.987654"),
            True,
            dt.date(9999, 12, 31),
            dt.datetime(9999, 12, 31, 23, 59, 59, 999999),
        ),
        (
            None,
            -9223372036854775808,
            None,
            False,
            dt.date(1, 1, 1),
            dt.datetime(1, 1, 1, 0, 0, 0),
        ),
        (
            "héllo 日本語 \U0001f389",
            None,
            Decimal("-99999999999999.999999"),
            None,
            dt.date(2000, 1, 1),
            None,
        ),
    ]


def _rows() -> list[tuple]:
    """The full fuzz corpus: the adversarial rows, then N random ones."""
    rng = random.Random(SEED)
    rows = list(_adversarial_rows())
    for _ in range(N_RANDOM):
        rows.append(
            (
                _rand_string(rng) if rng.random() > 0.1 else None,
                rng.randint(-(2**63), 2**63 - 1) if rng.random() > 0.1 else None,
                _rand_decimal(rng) if rng.random() > 0.1 else None,
                rng.choice([True, False]) if rng.random() > 0.1 else None,
                _rand_timestamp(rng).date() if rng.random() > 0.1 else None,
                _rand_timestamp(rng) if rng.random() > 0.1 else None,
            )
        )
    return rows


ROWS = _rows()


# --------------------------------------------------------------------------
# Fixtures: build an identical fuzz table in each engine.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def duck_fuzz(tmp_path_factory: pytest.TempPathFactory) -> Dialect:
    """Load the fuzz corpus into a private DuckDB file, then read it back."""
    import duckdb
    from conftest import _duckdb_available

    ok, why = _duckdb_available()
    if not ok:
        pytest.skip(why)

    path = str(tmp_path_factory.mktemp("fuzz") / "fuzz.duckdb")
    con = duckdb.connect(path)
    try:
        cols = ", ".join(f"{n} {t[1]}" for n, *t in COLUMNS)
        con.execute(f"create table {FUZZ_TABLE} ({cols})")
        placeholders = ", ".join(["?"] * len(COLUMNS))
        con.executemany(
            f"insert into {FUZZ_TABLE} values ({placeholders})",
            [(i, *row) for i, row in enumerate(ROWS)],
        )
    finally:
        con.close()

    dialect = open_duckdb(path, side="B")
    yield dialect
    dialect.close()


@pytest.fixture(scope="module")
def pg_fuzz(pg_url: str) -> Dialect:
    """Load the fuzz corpus into a private PostgreSQL schema, then read it back."""
    import psycopg

    con = psycopg.connect(pg_url, autocommit=True)
    try:
        con.execute(f"drop schema if exists {FUZZ_SCHEMA} cascade")
        con.execute(f"create schema {FUZZ_SCHEMA}")
        cols = ", ".join(f"{n} {t[0]}" for n, *t in COLUMNS)
        con.execute(f"create table {FUZZ_SCHEMA}.{FUZZ_TABLE} ({cols})")
        placeholders = ", ".join(["%s"] * len(COLUMNS))
        with con.cursor() as cur:
            cur.executemany(
                f"insert into {FUZZ_SCHEMA}.{FUZZ_TABLE} values ({placeholders})",
                [(i, *row) for i, row in enumerate(ROWS)],
            )
    finally:
        con.close()

    dialect = open_pg(pg_url, side="A")
    yield dialect
    dialect.close()


def _qualified(dialect: Dialect) -> str:
    """The fuzz table, qualified for whichever engine this is."""
    schema = FUZZ_SCHEMA if dialect.name == "postgres" else "main"
    return f"{schema}.{FUZZ_TABLE}"


def _column(dialect: Dialect, name: str) -> Column:
    """The introspected Column of the given name on this engine's fuzz table."""
    for col in dialect.columns(_qualified(dialect)):
        if col.name == name:
            return col
    raise AssertionError(f"no column {name!r} on side {dialect.side}")


# --------------------------------------------------------------------------
# The differential assertions.
# --------------------------------------------------------------------------


@pytest.mark.postgres
@pytest.mark.parametrize("column", VALUE_COLUMNS)
def test_random_values_normalize_identically_across_engines(pg_fuzz, duck_fuzz, column):
    """Every fuzzed value renders to byte-identical canonical text on both engines.

    One column at a time, all rows at once. A single disagreeing row - a lost
    microsecond, a NULL that rendered as SQL NULL instead of the sentinel, a
    Unicode byte one engine folded - fails the test and names the row.
    """
    pg_col = _column(pg_fuzz, column)
    duck_col = _column(duck_fuzz, column)
    pg_rows = pg_fuzz.query(
        f"select id, {pg_fuzz.normalize(pg_col)} "
        f"from {pg_fuzz.qualify(_qualified(pg_fuzz))} order by id"
    )
    duck_rows = duck_fuzz.query(
        f"select id, {duck_fuzz.normalize(duck_col)} "
        f"from {duck_fuzz.qualify(_qualified(duck_fuzz))} order by id"
    )
    assert len(pg_rows) == len(duck_rows) == len(ROWS)
    for (pid, pval), (did, dval) in zip(pg_rows, duck_rows, strict=True):
        assert pid == did
        assert pval == dval, (
            f"column {column!r} row {pid}: postgres rendered {pval!r}, "
            f"duckdb rendered {dval!r} for input {ROWS[pid][VALUE_COLUMNS.index(column)]!r}"
        )


@pytest.mark.postgres
def test_random_rows_hash_identically_across_engines(pg_fuzz, duck_fuzz):
    """The whole fuzzed row folds to the same 60-bit hash on both engines.

    This is the row-level contract, not just per-column: the concatenation, the
    separator, the coalesce, and the fold all have to agree at once, over every
    generated row.
    """
    pg_cols = sorted(
        (c for c in pg_fuzz.columns(_qualified(pg_fuzz)) if c.name != "id"),
        key=lambda c: c.name,
    )
    duck_cols = sorted(
        (c for c in duck_fuzz.columns(_qualified(duck_fuzz)) if c.name != "id"),
        key=lambda c: c.name,
    )
    pg_rows = pg_fuzz.query(
        f"select id, {pg_fuzz.row_hash(pg_cols)} "
        f"from {pg_fuzz.qualify(_qualified(pg_fuzz))} order by id"
    )
    duck_rows = duck_fuzz.query(
        f"select id, {duck_fuzz.row_hash(duck_cols)} "
        f"from {duck_fuzz.qualify(_qualified(duck_fuzz))} order by id"
    )
    assert len(pg_rows) == len(duck_rows) == len(ROWS)
    for (pid, phash), (did, dhash) in zip(pg_rows, duck_rows, strict=True):
        assert pid == did
        assert int(phash) == int(dhash), (
            f"row {pid} hashed to {phash} on postgres, {dhash} on duckdb: "
            f"input {ROWS[pid]!r}"
        )
        assert 0 <= int(phash) < 2**60, f"row {pid} hash out of 60-bit range"


@pytest.mark.postgres
def test_the_whole_engine_calls_two_fuzzed_tables_identical(pg_fuzz, duck_fuzz):
    """End to end, over fuzzed data: the same rows in Postgres and DuckDB match.

    Not just the encoding helpers - the real `diff()` walk, hashing pushed into
    both engines and the key range bisected, over a table of hostile values.
    Identical data must download zero rows and report identical; if any fuzzed
    value hashed differently across engines, the walk would chase a phantom
    difference and this would fail.
    """
    result = diff(pg_fuzz, duck_fuzz, _qualified(pg_fuzz), _qualified(duck_fuzz), "id")
    assert result.identical, f"fuzzed tables reported different: {result.diffs[:5]}"
    assert result.stats.rows_downloaded == 0


@pytest.mark.postgres
def test_a_single_planted_change_in_fuzzed_data_is_found(pg_fuzz, tmp_path):
    """A parity tool that never plants a difference proves nothing (CLAUDE.md 8).

    Build a fresh DuckDB copy of the fuzz corpus with exactly one row's string
    column changed, diff the untouched Postgres table against it, and confirm
    the real walk reports precisely that row and names the changed column - on
    hostile fuzzed data, across two engines. Building a fresh copy (rather than
    mutating a table a dialect already holds open) keeps the change visible: a
    read-only dialect pins a snapshot at connect time and would not see a
    mid-session write from another connection.
    """
    import duckdb

    victim = 3  # an adversarial row, guaranteed present
    path = str(tmp_path / "perturbed.duckdb")
    con = duckdb.connect(path)
    try:
        cols = ", ".join(f"{n} {t[1]}" for n, *t in COLUMNS)
        con.execute(f"create table {FUZZ_TABLE} ({cols})")
        placeholders = ", ".join(["?"] * len(COLUMNS))
        rows = []
        for i, row in enumerate(ROWS):
            r = list(row)
            if i == victim:
                r[VALUE_COLUMNS.index("s")] = "PERTURBED"
            rows.append((i, *r))
        con.executemany(f"insert into {FUZZ_TABLE} values ({placeholders})", rows)
    finally:
        con.close()

    perturbed = open_duckdb(path, side="B")
    try:
        result = diff(pg_fuzz, perturbed, _qualified(pg_fuzz), f"main.{FUZZ_TABLE}", "id")
        assert [(d.key, d.kind) for d in result.diffs] == [(victim, "different")], (
            f"expected exactly row {victim} to differ, got "
            f"{[(d.key, d.kind) for d in result.diffs]}"
        )
        assert "s" in result.diffs[0].columns
    finally:
        perturbed.close()
