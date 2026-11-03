"""Known conformance findings: the disagreements the sweep tolerates.

Each entry is a confirmed finding, pinned by its own strict-xfail test in
`test_conformance_findings.py` (the red-team convention: it fails loudly the
day the bug is fixed, so the entry is removed rather than left to rot). The
sweep in `test_conformance.py` fails on any disagreement *not* listed here,
so a new one can never hide behind an old one.

Keys are (engine, native type, value group); a finding covers every value
of that group the sweep found disagreeing, and `report.py` lists them.
"""

from __future__ import annotations

#: COV id -> one-paragraph statement, as the report prints it.
FINDINGS: dict[str, str] = {
    "COV-1": (
        "FIXED 2026-11-03 - casts TIMESTAMP_S/MS to TIMESTAMP before strftime. Was: "
        "DuckDB TIMESTAMP_S and TIMESTAMP_MS values outside 1677-09-22 .. "
        "2262-04-11 make `normalize` raise `Could not convert Timestamp(US) to "
        "Timestamp(NS)`: `strftime` binds its TIMESTAMP_NS overload for these "
        "types, so year 1, 1000 or 9999 kills the whole diff. Loud, not a false "
        "match; `cast(c as timestamp)` first would render them."
    ),
    "COV-2": (
        "FIXED 2026-11-03 - one rule: sub-microsecond digits are compared iff both sides hold them (`Column.sub_micro`, set by the engine), and truncated with a caveat otherwise. Was: "
        "DuckDB TIMESTAMP_NS renders the nanosecond digits of a value that has "
        "them (`...00.123456700`), while the rules (`parity.reference`), SQL "
        "Server datetime2(7) and Snowflake TIMESTAMP_NTZ(9) truncate to the "
        "microsecond (`...00.123456`). The same sub-microsecond instant held on "
        "DuckDB and on SQL Server or Snowflake is therefore reported as "
        "different - a false alarm, not a false match - and the per-engine "
        "check disagrees with the rules on every such value."
    ),
    "COV-3": (
        "FIXED 2026-11-03 - the DuckDB sample reads dates and timestamps as DuckDB's own text. Was: "
        "DuckDB sample path: the driver returns DATE/TIMESTAMP `infinity` as "
        "9999-12-31 (23:59:59.999999) and `-infinity` as 0001-01-01, so the "
        "independent sample check reduces +infinity and 9999-12-31 to the same "
        "token. `normalize` itself is right (`infinity`); the second opinion "
        "is blind to the difference and reports a false verification failure "
        "for any infinite value."
    ),
    "COV-4": (
        "FIXED 2026-11-03 - the reference parses DuckDB's `(BC)` as well as PostgreSQL's ` BC`. Was: "
        "DuckDB sample path: a BC date or timestamp comes back from the driver "
        "as text in DuckDB's spelling, `0044-03-15 (BC)`, which "
        "`parity.reference` does not parse (it reads PostgreSQL's ` BC` "
        "suffix), so every BC value is left unchecked by the sample check."
    ),
    "COV-5": (
        "FIXED 2026-11-03 - as COV-3: the text carries all nine digits, and the reference truncates. Was: "
        "DuckDB sample path: the driver rounds a TIMESTAMP_NS to the nearest "
        "microsecond where the rules truncate, so "
        "`1969-12-31 23:59:59.9999999` is read as `1970-01-01 00:00:00`: the "
        "sample check's raw value is not the stored value."
    ),
    "COV-6": (
        "FIXED 2026-11-03 - Snowflake TIME renders `to_char(c, 'HH24:MI:SS.FF6')`, the TIME rule every engine now follows (`canonical_time_text`). Was: "
        "Snowflake TIME (UNKNOWN-mapped) renders through `cast(c as varchar)`, "
        "which follows TIME_OUTPUT_FORMAT - `HH24:MI:SS` by default - and drops "
        "the fractional seconds: 12:34:56.5 and 12:34:56.123456 render "
        "`12:34:56`, exactly as 12:34:56 does there and on PostgreSQL, DuckDB "
        "and BigQuery. A FALSE MATCH, Snowflake to Snowflake and Snowflake to "
        "any engine."
    ),
    "COV-7": (
        "FIXED 2026-11-03 - DuckDB TIMETZ renders `canonical_timetz_text` from its "
        "clock fields and `datepart('timezone')`, the TIMETZ rule PostgreSQL follows "
        "too; the sample reads the driver's datetime.time, which carries the offset "
        "whole. Was: DuckDB TIME WITH TIME ZONE (UNKNOWN-mapped) rendered through "
        "its varchar cast, which drops an offset's minutes when it has seconds: "
        "12:00:00+05:00:30 printed `12:00:00+05:30`, exactly as 12:00:00+05:30 does "
        "(offsets 18030 s and 19800 s; DuckDB's own `=` calls them different). A "
        "FALSE MATCH, DuckDB to DuckDB and DuckDB to PostgreSQL."
    ),
    "COV-8": (
        "FIXED 2026-11-03 - a sql_variant float or real renders its IEEE bits in "
        "hex (`canonical_variant_text`). Was: style 3, which raised `Arithmetic "
        "overflow error converting expression to data type nvarchar` for -0.0 and "
        "for every negative double below 1e-99 (its text would be 24 characters), "
        "killing the whole diff on an ordinary value - loud, not a false match - "
        "and is not correctly rounded (1626983080611305.75 printed "
        "`1.6269830806113057e+015`; the nearest 17 digits end in 8), so no rule "
        "could state it."
    ),
}

#: (engine, native, group) -> COV id, for disagreements the sweep tolerates.
DISAGREE: dict[tuple[str, str, str], str] = {}

#: (engine, native, group) -> COV id, for values the sample path leaves unchecked.
UNCHECKED: dict[tuple[str, str, str], str] = {}

#: (engine, native, value key) -> why, for documented refusals of a value
#: that is not in a must-refuse group.
REFUSED: dict[tuple[str, str, str], str] = {
    ("bigquery", "NUMERIC", "d:99999999999999999999999999999.999999999"): (
        "BigQuery's ROUND raises `numeric overflow` when rounding carries past "
        "NUMERIC's maximum (CLAUDE.md 4.2): loud, as documented"
    ),
    ("bigquery", "NUMERIC", "d:-99999999999999999999999999999.999999999"): (
        "as above, negative"
    ),
}

#: (family, value key) -> COV id, for cross-engine splits on ruled types.
SPLITS: dict[str, str] = {}

#: UNKNOWN-mapped natives with a confirmed collision on one engine.
COLLISIONS: dict[tuple[str, str], str] = {}
