"""An in-memory ``Dialect`` so the bisection engine can be tested with no database.

Two things make this worth the code:

1. It proves the engine really is engine-agnostic. ``FakeDialect.query`` raises,
   so if the engine ever reached past the dialect contract to build SQL itself,
   every test here would fail loudly.
2. It counts round trips and rows served, which is how the "downloads zero rows
   on identical tables" and "queries grow logarithmically" claims get asserted
   rather than assumed.

The bucket arithmetic below deliberately mirrors the SQL expression in
``Dialect.segment_checksums`` character for character in intent:
``(key - lo) * n / (hi - lo)`` with truncating division. If the two ever drift,
``test_engine.py`` catches it - that is the point of the property test there.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any

from parity.dialects.base import HASH_HEX_CHARS, NULL_SENTINEL, Dialect
from parity.types import Column, KeyStats, LogicalType


def row_hash(text: str) -> int:
    """The same fold the real dialects perform, in Python.

    15 hex characters = 60 bits, matching CLAUDE.md section 4.1.
    """
    # MD5 here is the checksum both engines compute, chosen for
    # cross-engine agreement, not for any security property.
    digest = hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()
    return int(digest[:HASH_HEX_CHARS], 16)


class Table:
    """Rows addressed by an integer key.

    Subclasses either hold a dict or generate rows on demand; the generated
    flavour is what makes a million-row test run in a second and a few
    megabytes instead of gigabytes.
    """

    columns: list[Column]

    def keys_in(self, lo: int, hi: int) -> Iterable[int]:
        """Every key in the half-open range [lo, hi)."""
        raise NotImplementedError

    def row(self, key: int) -> tuple[str, ...]:
        """The values for one key, in this table's own column order."""
        raise NotImplementedError

    def all_keys(self) -> Iterable[int]:
        """Every key in the table."""
        raise NotImplementedError

    # -- derived -----------------------------------------------------------

    def key_stats(self) -> KeyStats:
        """Range and counts for the key column, as a real dialect would report."""
        keys = list(self.all_keys())
        if not keys:
            return KeyStats(None, None, 0, 0)
        return KeyStats(min(keys), max(keys), len(keys), len(set(keys)))

    def text(self, key: int, columns: Sequence[Column]) -> str:
        """Canonical row text, restricted to ``columns`` and in their order."""
        if not columns:
            return ""  # mirrors Dialect.row_text's empty-column fallback
        index = {c.name: i for i, c in enumerate(self.columns)}
        values = self.row(key)
        return "\x1f".join(values[index[c.name]] for c in columns)


class DictTable(Table):
    """A handful of rows spelled out literally. For readable small tests."""

    def __init__(self, columns: list[Column], rows: dict[int, tuple[str, ...]]):
        """Hold a literal set of rows, keyed by their key value."""
        self.columns = columns
        self.rows = dict(rows)

    def keys_in(self, lo: int, hi: int) -> Iterable[int]:
        """Keys inside [lo, hi). Scans the dict, which is fine at this size."""
        return [k for k in self.rows if lo <= k < hi]

    def row(self, key: int) -> tuple[str, ...]:
        """The stored values for one key."""
        return self.rows[key]

    def all_keys(self) -> Iterable[int]:
        """Every key that was spelled out."""
        return list(self.rows)


class SyntheticTable(Table):
    """Keys ``1..n`` generated on the fly, with planted differences.

    Memory is O(number of planted differences), not O(n), so a comparison of
    two million-row tables costs a few kilobytes.
    """

    def __init__(
        self,
        n: int,
        columns: list[Column] | None = None,
        changed: dict[int, tuple[str, ...]] | None = None,
        deleted: Iterable[int] = (),
        extra: dict[int, tuple[str, ...]] | None = None,
    ):
        """Describe a table of `n` rows plus whatever differences are planted.

        `changed` replaces a row's values, `deleted` removes keys, and `extra`
        adds keys outside 1..n. Nothing is materialised.
        """
        self.n = n
        self.columns = columns or [
            Column("amount", LogicalType.DECIMAL, "decimal(12,2)"),
            Column("status", LogicalType.STRING, "varchar"),
        ]
        self.changed = dict(changed or {})
        self.deleted = set(deleted)
        self.extra = dict(extra or {})

    # The generated row for a key. Deterministic, so both sides agree unless a
    # difference was deliberately planted.
    def _generated(self, key: int) -> tuple[str, ...]:
        """The default row for a key. Deterministic, so both sides agree."""
        return (f"{key % 1000}.{key % 100:02d}", f"status-{key % 7}")

    def row(self, key: int) -> tuple[str, ...]:
        """Values for one key: planted if it was planted, generated otherwise."""
        if key in self.extra:
            return self.extra[key]
        if key in self.changed:
            return self.changed[key]
        return self._generated(key)

    def keys_in(self, lo: int, hi: int) -> Iterator[int]:
        """Keys in [lo, hi), yielded lazily so a million rows cost nothing."""
        start, stop = max(1, lo), min(self.n + 1, hi)
        for k in range(start, stop):
            if k not in self.deleted:
                yield k
        for k in sorted(self.extra):
            if lo <= k < hi and not (1 <= k <= self.n):
                yield k

    def all_keys(self) -> Iterator[int]:
        """Every key, including any planted outside the generated range."""
        return self.keys_in(1, max(self.n, *self.extra) + 1 if self.extra else self.n + 1)


class FakeDialect(Dialect):
    """A ``Dialect`` over a :class:`Table`. Serves one table only."""

    name = "fake"

    def __init__(
        self,
        table: Table,
        side: str = "A",
        float_scale: int = 6,
        key_type_override: Column | None = None,
        predicates: dict[str, Callable[[int], bool]] | None = None,
    ):
        """Wrap a Table so the engine can drive it as if it were a database.

        `predicates` stands in for SQL row filters: a `where` string the
        engine passes is looked up here and applied to each key, and every
        call records the filter it received in `wheres`, so a test can assert
        that each row-reading call was filtered.
        """
        super().__init__(float_scale=float_scale, side=side)
        self.table = table
        self.key_column = key_type_override or Column("id", LogicalType.INTEGER, "bigint")
        #: Round trips this side served. The engine's own counter is checked
        #: against the sum of both sides' counters.
        self.queries = 0
        #: Rows actually handed back, i.e. what crossed the "network".
        self.rows_served = 0
        self.predicates = dict(predicates or {})
        #: (method, where) for every row-reading call, in order.
        self.wheres: list[tuple[str, str | None]] = []

    # -- contract ----------------------------------------------------------

    def connect(self, connection_string: str) -> None:  # pragma: no cover
        """No-op: the data is already in memory."""

    def close(self) -> None:
        """No-op: there is nothing to release."""

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        """Always raises - reaching here means the engine built SQL itself.

        This is the load-bearing part of the fake. The engine is supposed to
        go through the three operations below and never compose SQL of its
        own, and a passing run is the proof that it did not.
        """
        raise AssertionError(
            "the engine built SQL directly instead of going through the "
            f"dialect contract: {sql!r}"
        )

    def columns(self, table: str) -> list[Column]:
        """The key column followed by the table's own columns."""
        return [self.key_column, *self.table.columns]

    def quote(self, identifier: str) -> str:
        """Quote an identifier. Never reaches a database, but keeps shapes real."""
        return f'"{identifier}"'

    def normalize(self, column: Column) -> str:  # pragma: no cover
        """Unused: this fake compares Python values, not rendered SQL."""
        return f"norm({column.name})"

    def hash_expr(self, text_expr: str) -> str:  # pragma: no cover
        """Unused: hashing happens in Python here, via `row_hash`."""
        return f"hash({text_expr})"

    def int_div(self, numerator: str, denominator: str) -> str:  # pragma: no cover
        """Unused: bucket arithmetic is done directly in `segment_checksums`."""
        return f"(({numerator}) // ({denominator}))"

    def sum_wide(self, expr: str) -> str:  # pragma: no cover
        """Unused: Python integers are already arbitrary precision."""
        return f"sum({expr})"

    def wide_int(self, expr: str) -> str:  # pragma: no cover
        """Unused: Python integers cannot overflow."""
        # Python integers are already arbitrary precision.
        return f"({expr})"

    # -- the three operations the engine actually calls ---------------------

    def _keep(self, method: str, where: str | None) -> Callable[[int], bool] | None:
        """Record the filter a call received; return it as a key test, or None
        for no filter (so an unfiltered walk pays nothing for it)."""
        self.wheres.append((method, where))
        if where is None:
            return None
        return self.predicates[where]

    @staticmethod
    def _only(keep: Callable[[int], bool] | None, keys: Iterable[int]) -> Iterable[int]:
        """The keys a filter keeps; all of them when there is none."""
        return keys if keep is None else (k for k in keys if keep(k))

    def key_stats(self, table: str, key: str, where: str | None = None) -> KeyStats:
        """Key range and counts. Counts as one round trip."""
        self.queries += 1
        keep = self._keep("key_stats", where)
        if keep is None:
            return self.table.key_stats()
        keys = [k for k in self.table.all_keys() if keep(k)]
        if not keys:
            return KeyStats(None, None, 0, 0)
        return KeyStats(min(keys), max(keys), len(keys), len(set(keys)))

    def segment_checksums(
        self,
        table: str,
        key: str,
        columns: Sequence[Column],
        lo: int,
        hi: int,
        n_segments: int,
        where: str | None = None,
    ) -> dict[int, tuple[int, int]]:
        """Row count and checksum per bucket, computed in Python.

        The bucket arithmetic mirrors the SQL expression exactly - truncating
        integer division - because a test compares the two.
        """
        self.queries += 1
        keep = self._keep("segment_checksums", where)
        span = hi - lo
        out: dict[int, list[int]] = {}
        for k in self._only(keep, self.table.keys_in(lo, hi)):
            # Exactly the SQL expression: truncating integer division.
            bucket = ((k - lo) * n_segments) // span
            acc = out.setdefault(bucket, [0, 0])
            acc[0] += 1
            # Mirror Dialect._segment_sql: fold the key into the hash so a
            # same-content insert and delete in one bucket cannot cancel. The
            # integer key's canonical text is str(k), and row_text prepends it
            # to the column text with the field separator.
            col_text = self.table.text(k, columns)
            combined = str(k) if not columns else f"{k}\x1f{col_text}"
            acc[1] += row_hash(combined)
        return {i: (c, s) for i, (c, s) in out.items()}

    def fetch_range(
        self,
        table: str,
        key: str,
        columns: Sequence[Column],
        lo: int,
        hi: int,
        where: str | None = None,
    ) -> dict[int, tuple[str, ...]]:
        """Every row in [lo, hi), and the only thing that counts as downloaded."""
        self.queries += 1
        keep = self._keep("fetch_range", where)
        index = {c.name: i for i, c in enumerate(self.table.columns)}
        out: dict[int, tuple[str, ...]] = {}
        for k in sorted(self._only(keep, self.table.keys_in(lo, hi))):
            if k in out:
                raise self._err(f"duplicate key {k} in {table}")
            values = self.table.row(k)
            out[k] = tuple(values[index[c.name]] for c in columns)
        self.rows_served += len(out)
        return out

    def fetch_sample(
        self,
        table: str,
        key: str,
        columns: Sequence[Column],
        modulus: int,
        residue: int,
        where: str | None = None,
    ) -> dict[int, tuple[Any, ...]]:
        """The independent sample: rows whose key hash is `residue` mod
        `modulus`, as raw values. Counted apart (`samples`, `rows_sampled`),
        so the walk's own query and download counts stay what they assert."""
        self.samples = getattr(self, "samples", 0) + 1
        keep = self._keep("fetch_sample", where)
        index = {c.name: i for i, c in enumerate(self.table.columns)}
        out: dict[int, tuple[Any, ...]] = {}
        for k in self._only(keep, self.table.all_keys()):
            if modulus > 1 and row_hash(str(k)) % modulus != residue:
                continue
            values = self.raw_row(k)
            out[k] = tuple(values[index[c.name]] for c in columns)
        self.rows_sampled = getattr(self, "rows_sampled", 0) + len(out)
        return out

    def raw_row(self, key: int) -> tuple[Any, ...]:
        """A row's raw values for the sample check: its stored values, which
        in this fake are already the text the reference rules read. A test
        overrides this to make the raw values and the rendered text disagree,
        which is what a dialect bug looks like."""
        return self.table.row(key)


__all__ = [
    "NULL_SENTINEL",
    "DictTable",
    "FakeDialect",
    "SyntheticTable",
    "Table",
    "row_hash",
]
