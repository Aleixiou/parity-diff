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
    #: Time of day, to the microsecond: `HH:MM:SS.ffffff`. MySQL's TIME is a
    #: duration (up to +-838:59:59) and renders as one past the day; no other
    #: engine can hold those values, so they can only match MySQL.
    TIME = "time"
    #: Bytes, exactly: `\\x` + lower-case hex (the escaped spelling of
    #: PostgreSQL's bytea text, which is what PostgreSQL and MySQL rendered
    #: before the type had a rule, so their checksums did not move).
    BINARY = "binary"
    #: A JSON document, as its engine's normalised serialisation laid out one
    #: way everywhere (`canonical_json_text`). Engines normalise differently
    #: - key order, number spelling - so cross-engine equality holds only
    #: where their normal forms coincide, and the engine warns otherwise.
    JSON = "json"
    #: A bit string, as its digits at full width: `00000101` (PostgreSQL
    #: bit/varbit, DuckDB BIT, MySQL BIT(n)). Never a boolean: `1` is not
    #: `true`, and SQL Server's `bit` stays BOOLEAN.
    BITS = "bits"
    #: A calendar interval as its three exact components, `<months> mon
    #: <days> day <microseconds> us` (`canonical_interval_text`): `1 day` and
    #: `24:00:00` are stored differently and render differently.
    INTERVAL = "interval"
    #: A time of day with its UTC offset, `HH:MM:SS.ffffff+HH:MM`
    #: (`canonical_timetz_text`): local time and offset both kept.
    TIMETZ = "timetz"
    #: A one-dimensional list or array: each element by its own type's rule,
    #: each followed by a terminator, inside brackets (`canonical_list_text`).
    LIST = "list"
    #: A PostgreSQL inet/cidr value: its full address and prefix length,
    #: `10.0.0.1/32` (`canonical_inet_text`).
    INET = "inet"
    #: A SQL Server sql_variant: `<base type>:<exact value>`
    #: (`canonical_variant_text`). No other engine has the type.
    VARIANT = "variant"
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
    #: Compare digits past microseconds. Set by the engine on a TIMESTAMP
    #: column only when *both* sides can hold them (DuckDB TIMESTAMP_NS,
    #: SQL Server datetime2(7), Snowflake TIMESTAMP(7-9)): then a value with
    #: a sub-microsecond part renders all nine digits on either engine. When
    #: one side holds microseconds only, they are truncated, as that side's
    #: data already is, and a caveat says so (COV-2).
    sub_micro: bool = False


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
    #: The independent sample check's own cost, kept apart so `queries` and
    #: `rows_downloaded` still describe the checksum walk alone.
    sample_queries: int = 0
    rows_sampled: int = 0


#: Confidence level of the risk figures each run prints (`max_hidden_fraction`).
RISK_CONFIDENCE = 0.999

#: Upper bound on the chance that one bucket holding a real difference still
#: sums to the other side's checksum: row hashes are 60-bit MD5 prefixes, so
#: for data not crafted to collide (CLAUDE.md 4.11) a differing sum matches
#: with probability at most 2^-60, about 8.7e-19.
CHECKSUM_COLLISION_BOUND = 2.0**-60


@dataclass
class Verification:
    """The independent sample check: rows re-compared from raw driver values.

    After the checksum walk, a sample of rows is downloaded from both sides as
    the drivers return them and compared in Python by the documented rules
    (`parity.reference`), with none of the dialects' canonical-text SQL. A
    false match needs that SQL to render two different values alike; this
    check cannot be fooled the same way, so a disagreement here means the walk
    itself is wrong for these data and the tables are not called identical.
    """

    #: Rows sampled: those whose key hash is `residue` modulo `modulus`, so
    #: about one in `modulus`; 1 means every row was compared.
    modulus: int
    residue: int
    rows_a: int
    rows_b: int
    #: Columns compared by value, and columns whose type the reference rules
    #: do not cover (structs, nested lists, ...) - those were not
    #: independently checked.
    checked: list[str] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)
    #: Rows the walk called equal that the raw values say differ, or keys one
    #: side's sample holds that the walk never reported. Any entry means the
    #: comparison cannot be trusted for this data: never identical.
    failures: list[str] = field(default_factory=list)
    #: Differences the walk reported that the raw values do not show - a false
    #: alarm in the walk or a limit of the reference; reported, not blocking.
    notes: list[str] = field(default_factory=list)
    #: Why the check could not run, if it could not. Blocking: the run asked
    #: for a second method and did not get one, so the result is not proven
    #: and never identical (switch the check off with ``verify_sample=0`` to
    #: rest the verdict on the checksums alone, deliberately).
    error: str | None = None

    def max_hidden_fraction(self, confidence: float = RISK_CONFIDENCE) -> float | None:
        """Upper bound on the share of rows the walk could have got wrong.

        Each row is in the sample with probability 1/`modulus` (the residue is
        drawn at random each run, and the key hash spreads keys evenly over
        the residues). If a fraction p of rows were rendered wrongly, the
        chance that none of the n sampled rows is one of them is (1 - p)^n;
        so after n rows with no disagreement, p < 1 - (1 - confidence)^(1/n)
        at that confidence - the exact binomial bound (for 95% this is the
        "rule of three", about 3/n). 0.0 when every row was compared (modulus
        1). None when the check failed, could not run, or sampled nothing.
        """
        if self.failures or self.error:
            return None
        if self.modulus == 1:
            return 0.0
        n = min(self.rows_a, self.rows_b)
        if n == 0:
            return None
        return float(1.0 - (1.0 - confidence) ** (1.0 / n))


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
    #: The independent sample check, or None when it was switched off
    #: (``verify_sample=0``) or the walk stopped early.
    verification: Verification | None = None
    #: The row filter each side was read through (`--where`), or None for
    #: the whole table. A filtered verdict covers the filtered rows only, so
    #: the output always states both - a filtered "identical" must never be
    #: read as one about the whole table.
    where_a: str | None = None
    where_b: str | None = None
    #: Renamed columns (`--map`), side A's stored name to side B's, for the
    #: compared and key columns that were paired by the map rather than by
    #: name. Differences are reported under side A's name.
    column_map: dict[str, str] = field(default_factory=dict)

    #: The sample size the run asked for (``verify_sample``); 0 means the
    #: check was deliberately switched off, which the output says. None when
    #: the result was not built by ``diff``.
    verify_sample: int | None = None

    @property
    def filtered(self) -> bool:
        """Whether either side was read through a row filter."""
        return bool(self.where_a or self.where_b)

    @property
    def identical(self) -> bool:
        """Proven to match: no differences, the whole key space walked, every
        column on both sides compared, every checksum mismatch explained, and
        the independent sample check - when it ran - neither contradicting the
        walk nor failing to run. A check that was asked for and could not run
        leaves the result unproven, not identical."""
        return (
            not self.diffs
            and not self.truncated
            and not self.schema_differences
            and not self.unexplained
            and not (self.verification and self.verification.failures)
            and not (self.verification and self.verification.error)
        )

    def by_kind(self, kind: str) -> list[RowDiff]:
        """The differences of one kind: only_in_a, only_in_b or different."""
        return [d for d in self.diffs if d.kind == kind]
