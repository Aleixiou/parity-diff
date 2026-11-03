"""Core value types shared across dialects and the diff engine.

The original build plan sketched a ``TableRef`` and a ``Segment`` dataclass. Neither
survived contact with the implementation: the engine takes a dialect, a
table name and a key as separate arguments rather than bundling them, and a
segment is a plain ``(lo, hi)`` tuple whose checksums live in the dict the
dialect returns. They were carried unused for a while, which is worse than
not having them - an unused public dataclass invites someone to build on it
and then drift out of sync with what the code really does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class LogicalType(str, Enum):
    """Engine-independent type categories.

    Every physical column type a dialect knows about is mapped onto one of
    these, and each one has exactly one canonical text encoding (see
    ``Dialect.normalize``). Two rows are equal iff their canonical encodings
    are byte-identical, so this enum is where cross-engine correctness lives.
    """

    INTEGER = "integer"
    DECIMAL = "decimal"
    FLOAT = "float"
    BOOLEAN = "boolean"
    STRING = "string"
    DATE = "date"
    TIMESTAMP = "timestamp"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Column:
    name: str
    logical_type: LogicalType
    raw_type: str = ""
    #: Whether the column holds an instant (True) or naive wall-clock time
    #: (False). None means "infer from `raw_type`", which works wherever the
    #: engine spells it `... with time zone` (PostgreSQL, DuckDB). BigQuery
    #: does not: its instant type is plainly `TIMESTAMP` and its naive one is
    #: `DATETIME`, so it sets this explicitly rather than have the engine
    #: guess from a name that means the opposite elsewhere.
    tz_aware: bool | None = None


@dataclass(frozen=True)
class KeySpec:
    """How rows are matched up between the two sides.

    A single integer column is used directly: it buckets cleanly and the SQL is
    exactly what it always was. Anything else - a uuid, a natural string key, or
    several columns together - is *hashed* into a 60-bit integer purely so the
    bisection has something to divide.

    The hash is only ever used to choose buckets. Row identity stays the
    original key text, because a 60-bit hash has a real collision probability
    over a large table, and two unrelated rows sharing a bucket must not be
    mistaken for the same row. Bucketing may collide harmlessly; identity may
    not collide at all.
    """

    columns: tuple[Column, ...]
    #: True when the key has to be hashed to produce a bucketing integer.
    hashed: bool

    @property
    def names(self) -> list[str]:
        """The key column names, in the order the user gave them."""
        return [c.name for c in self.columns]

    @property
    def label(self) -> str:
        """How to name this key in an error message."""
        if len(self.columns) == 1:
            return repr(self.columns[0].name)
        return " + ".join(repr(n) for n in self.names)


@dataclass(frozen=True)
class KeyStats:
    """What one side reports about its key column, from a single scan.

    ``rows`` and ``distinct`` ride along with ``min``/``max`` because the
    min/max query already has to visit the key column. Getting uniqueness for
    free matters: a non-unique key silently collapses rows during comparison
    and makes differences vanish, which is the most dangerous failure this
    tool can have.
    """

    lo: int | None
    hi: int | None
    rows: int
    distinct: int
    #: Rows whose key is not NULL. ``None`` means the dialect did not measure
    #: it, in which case no NULL keys are assumed.
    non_null: int | None = None

    @property
    def empty(self) -> bool:
        """True when the table holds no rows at all."""
        return self.rows == 0

    @property
    def null_keys(self) -> int:
        """How many rows have no key. Such a row cannot be matched to anything."""
        return 0 if self.non_null is None else self.rows - self.non_null

    @property
    def has_null_keys(self) -> bool:
        """Whether any key is NULL. Checked before uniqueness, deliberately."""
        return self.null_keys > 0

    @property
    def has_duplicate_keys(self) -> bool:
        """Whether the key repeats, which would collapse rows during comparison."""
        # Compare like with like: `count(distinct k)` ignores NULLs, so
        # measuring it against `count(*)` would report every NULL key as a
        # duplicate and send the reader hunting for duplicates that do not
        # exist. NULL keys are diagnosed separately.
        comparable = self.rows if self.non_null is None else self.non_null
        return comparable != self.distinct


@dataclass
class RowDiff:
    #: The row's key as the user would recognise it: an int for an integer
    #: key, otherwise the canonical text of the key column(s). Never a hash -
    #: a reader has to be able to go and look the row up.
    key: int | str
    kind: str  # "only_in_a" | "only_in_b" | "different"
    columns: list[str] = field(default_factory=list)
    values_a: dict[str, str] = field(default_factory=dict)
    values_b: dict[str, str] = field(default_factory=dict)


@dataclass
class DiffStats:
    queries: int = 0
    segments_checked: int = 0
    rows_downloaded: int = 0
    rows_compared_a: int = 0
    rows_compared_b: int = 0
    seconds: float = 0.0


@dataclass
class DiffResult:
    diffs: list[RowDiff]
    stats: DiffStats
    columns: list[Column]
    warnings: list[str] = field(default_factory=list)
    #: True when the walk stopped early (``max_diffs``), so ``diffs`` is a
    #: partial answer. CLAUDE.md section 8: never let an approximate result
    #: look exact. Callers must not read ``identical`` as "tables match" when
    #: this is set.
    truncated: bool = False
    #: Decimal places at which DECIMAL/FLOAT columns were compared. Surfaced
    #: in output so a reader knows the comparison was rounded, not exact.
    float_scale: int = 6
    #: Columns one side has and the other lacks, that the user did not set
    #: aside with ``exclude`` or ``columns``. Their contents were never
    #: compared, so the tables cannot be called identical however well the
    #: shared columns agree: a migration that dropped a column outright is the
    #: most destructive defect there is, and "no differences" over the columns
    #: that survived must not read as a pass.
    schema_differences: list[str] = field(default_factory=list)
    #: Key ranges whose checksums disagreed but whose downloaded rows could not
    #: account for it - they compared equal, or a side returned a different
    #: number of rows than its own checksum counted. The engines proved a
    #: difference exists there; something between the server and the local
    #: comparison (a driver dropping bytes, a lossy decode, a live table
    #: changing between queries) lost it. Never a reason to say "identical".
    unexplained: list[str] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        """Proven to match: no differences, the whole key space walked, every
        column on both sides compared, and every checksum mismatch explained."""
        return (
            not self.diffs
            and not self.truncated
            and not self.schema_differences
            and not self.unexplained
        )

    def by_kind(self, kind: str) -> list[RowDiff]:
        """The differences of one kind: only_in_a, only_in_b or different."""
        return [d for d in self.diffs if d.kind == kind]
