"""Write `docs/coverage.md` from the saved conformance runs.

    python tests/conformance/report.py [--results DIR] [--out docs/coverage.md]

The map is generated, never hand-edited: a cell says what was measured on
the last run of each engine - "N values, all agree", "k of N disagree" with
the first examples, "refused", or nothing at all when that engine has no
native type for the family. The UNKNOWN-mapped types get their own section:
which native types fall there on each engine, whether one engine renders two
of the sample values alike (a false-match risk no rule guards), and how
their text compares across engines.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path[:0] = [
    str(Path(__file__).resolve().parent),
    str(Path(__file__).resolve().parents[1]),
]

from cov_cross import collisions, compare, load_runs  # noqa: E402
from cov_harness import RESULTS  # noqa: E402
from cov_known import (  # noqa: E402
    COLLISIONS,
    DISAGREE,
    FINDINGS,
    REFUSED,
    SPLITS,
    UNCHECKED,
)
from cov_values import FORM_DEPENDENT, REFUSE, RULED, cross_group  # noqa: E402

ENGINE_ORDER = ["postgres", "duckdb", "mysql", "mssql", "bigquery", "snowflake", "oracle"]

#: Matrix rows: (family label, value groups in it).
FAMILIES: list[tuple[str, tuple[str, ...]]] = [
    ("INTEGER", ("int",)),
    ("DECIMAL", ("dec", "dec_wide")),
    ("FLOAT (double)", ("float",)),
    ("FLOAT (single)", ("float4",)),
    ("FLOAT past 2^116 (must refuse)", ("float_refuse",)),
    ("BOOLEAN", ("bool",)),
    ("STRING", ("str",)),
    ("STRING, fixed-width CHAR", ("char",)),
    ("STRING, uuid", ("uuid",)),
    ("DATE", ("date",)),
    ("DATE, infinity / BC", ("date_ext",)),
    ("TIMESTAMP, naive", ("ts",)),
    ("TIMESTAMP, infinity / BC", ("ts_ext",)),
    ("TIMESTAMP, sub-microsecond", ("ts_sub",)),
    ("TIMESTAMP, instant (tz-aware)", ("tstz",)),
    ("TIME", ("time",)),
    ("TIME, 24:00:00", ("time_ext",)),
    ("TIME, sub-microsecond", ("time_sub",)),
    ("TIME, MySQL duration", ("time_dur",)),
    ("BINARY", ("bytes",)),
    ("JSON, every engine alike", ("json",)),
    ("JSON, engine normal forms", ("json_forms",)),
    ("BITS", ("bits",)),
    ("INTERVAL", ("interval",)),
    ("TIMETZ", ("timetz",)),
    ("INET", ("inet",)),
    ("LIST of text", ("list_text",)),
    ("LIST of integers", ("list_int",)),
    (
        "LIST of dates, decimals, floats, booleans, intervals",
        ("list_date", "list_dec", "list_float", "list_bool", "list_interval"),
    ),
    ("LIST, PostgreSQL shapes (dimensions, lower bounds)", ("list_shaped",)),
    ("VARIANT (sql_variant)", ("variant",)),
]

#: Native types each dialect maps onto a logical type but this suite does not
#: load, and those that fall to UNKNOWN and are not loaded either. From the
#: dialects' `map_type*` functions and engine documentation, not measured.
NOT_LOADED: dict[str, dict[str, str]] = {
    "postgres": {
        "mapped": "serial, bigserial, int2/int4/int8 spellings (same types), "
        "char(n) widths other than 20, varchar without length, timestamp(p)/"
        "timestamptz(p) with p < 6, domains over any of these, bit(n) widths "
        "other than 8, arrays of element types other than integer, text, date, "
        "numeric, double precision, boolean and interval (each element follows "
        "its own type's rule); multi-dimensional arrays and arrays with a lower "
        "bound other than 1 of element types other than integer",
        "unknown": "xml, macaddr, point and other geometric types, tsvector, "
        'multiranges, composite types, oid, "char", hstore and citext '
        "(extensions), arrays of JSON, of enums and of any other unmapped type; "
        "a range and a jsonb array are loaded below",
    },
    "duckdb": {
        "mapped": "DECIMAL widths other than (4,1), (18,4), (38,9), (38,30); "
        "TIMESTAMP WITH TIME ZONE spellings; BYTEA/VARBINARY (reported as BLOB); "
        "lists of element types other than INTEGER, VARCHAR, DATE, DECIMAL(18,7), "
        "DOUBLE, BOOLEAN and INTERVAL; lists of TIMESTAMP_NS (refused: digits "
        "past the microsecond)",
        "unknown": "MAP, UNION (special-cased), VARIANT (special-cased), TIME_NS, "
        "fixed-size ARRAY, lists of structs, maps or JSON, BIGNUM/VARINT; a "
        "nested list and a STRUCT are loaded below",
    },
    "mysql": {
        "mapped": "tinytext, mediumtext, text, set, dec/fixed/numeric aliases, "
        "int unsigned, datetime/timestamp with precision < 6, binary(n) (its "
        "zero padding is data), tinyblob/mediumblob/longblob, time(1..5), "
        "bit(n) widths other than 1, 8 and 64",
        "unknown": "geometry and the spatial types (rendered as hex of the stored bytes)",
    },
    "mssql": {
        "mapped": "text, ntext, sysname, varchar(n)/nvarchar(n) with n < max, "
        "datetime2(p) with p < 6, datetimeoffset(p) with p < 7, varchar under a "
        "non-UTF-8 collation (tested elsewhere: tests/test_mssql.py), binary(n), "
        "rowversion/timestamp (not insertable), time(1..6)",
        "unknown": "xml, geography, geometry, hierarchyid; no JSON type (an "
        "nvarchar holding JSON is STRING)",
    },
    "bigquery": {
        "mapped": "INT64 aliases (INTEGER, SMALLINT, ...), BIGDECIMAL, "
        "parameterised NUMERIC(p,s) / STRING(n) / BYTES(n); a JSON null (the "
        "load job cannot store one; the NULL guard is pinned offline)",
        "unknown": "STRUCT, GEOGRAPHY, INTERVAL, RANGE, and ARRAY of those or "
        "of JSON (no cross-engine rule; rendered injectively: STRUCT field by "
        "field, GEOGRAPHY as WKB). ARRAY of ruled types follows the list rule, "
        "but a BigQuery table cannot store a NULL array or a NULL element",
    },
    "snowflake": {
        "mapped": "NUMBER(p,0) for p < 38 and NUMBER(p,s) for other s, INT/BIGINT "
        "aliases, CHAR/TEXT/STRING (aliases of VARCHAR), TIMESTAMP_NTZ/TZ/LTZ "
        "with precision < 9, DATETIME alias, TIME(0..8), OBJECT and ARRAY (JSON "
        "as VARIANT is), VARBINARY; no single-precision FLOAT exists",
        "unknown": "GEOGRAPHY, GEOMETRY, VECTOR, FILE",
    },
    "oracle": {
        "mapped": "NUMBER(p,0) for other p, NUMBER(p,-s), NUMBER(p,s) for other "
        "s, FLOAT(p) (a decimal NUMBER), VARCHAR2/CHAR/NVARCHAR2/NCHAR of other "
        "lengths, TIMESTAMP(p) for p other than 6 and 9, TIMESTAMP WITH TIME "
        "ZONE of other precisions, INTERVAL DAY TO SECOND(p) for p other than 6, "
        "RAW(n) for n < 2000; rows past 4,000 bytes (the PL/SQL hash path: "
        "tests/test_oracle.py)",
        "unknown": "ROWID, UROWID (text); LONG, LONG RAW, XMLTYPE, VECTOR, BFILE, "
        "SDO_GEOMETRY and object types (refused); JSON is loaded below",
    },
}


def _engines(runs: dict[str, dict[str, Any]]) -> list[str]:
    """Engines with a saved run, in the report's column order."""
    return [e for e in ENGINE_ORDER if e in runs] + sorted(set(runs) - set(ENGINE_ORDER))


def _cov_for(engine: str, c: dict[str, Any]) -> str | None:
    """The COV id or documented reason that explains a non-agreeing cell."""
    k = (engine, c["native"], c["group"])
    if c["status"] in ("disagree", "error"):
        return DISAGREE.get(k)
    if c["status"] == "unchecked":
        return UNCHECKED.get(k)
    if c["status"] == "refused" and c["group"] not in REFUSE:
        return "documented" if (engine, c["native"], c["key"]) in REFUSED else None
    return None


def _cell_text(engine: str, cells: list[dict[str, Any]]) -> str:
    """One matrix cell: what was measured for a family on one engine."""
    if not cells:
        return "\u2014"
    unruled = sorted({c["native"] for c in cells if c["status"] == "unruled"})
    cells = [c for c in cells if c["status"] != "unruled"]
    st = Counter(c["status"] for c in cells)
    natives = sorted({c["native"] for c in cells})
    n = sum(v for k, v in st.items() if k != "unstorable")
    parts = []
    if st["agree"] == n:
        parts.append(f"**{n} values, all agree**")
    else:
        parts.append(f"{st['agree']} of {n} agree")
        for status in ("disagree", "error", "unchecked", "refused"):
            if st[status]:
                covs = sorted(
                    {
                        _cov_for(engine, c) or "UNEXPLAINED"
                        for c in cells
                        if c["status"] == status
                    }
                )
                label = (
                    "refused (as required)"
                    if status == "refused"
                    and all(
                        c["group"] in REFUSE for c in cells if c["status"] == "refused"
                    )
                    else status
                )
                parts.append(
                    f"{st[status]} {label} ({', '.join(covs)})".replace(" (None)", "")
                )
    if st["unstorable"]:
        parts.append(f"{st['unstorable']} not storable in the type")
    text = "; ".join(parts) + "<br>" + ", ".join(f"`{x}`" for x in natives)
    if unruled:
        text += "<br>UNKNOWN-mapped, text only: " + ", ".join(f"`{x}`" for x in unruled)
    return text


def _public(detail: str) -> str:
    """An engine's error text, safe to publish: from the tool's own message
    when it has one, and never a URL - a BigQuery error carries the request
    URL, and with it the project id of whoever ran the sweep."""
    i = detail.find("parity:")
    text = detail[i:] if i >= 0 else detail.split(": ", 1)[-1]
    return re.sub(r"https?://\S+", "<url>", text)


def _refusal_cell(cells: list[dict[str, Any]]) -> str:
    """The must-refuse row: refused counts, with the first message."""
    if not cells:
        return "\u2014"
    refused = [c for c in cells if c["status"] == "refused"]
    other = [c for c in cells if c["status"] not in ("refused", "unstorable")]
    text = f"**{len(refused)} of {len(refused) + len(other)} refused**"
    if other:
        text += f"; {len(other)} rendered (WRONG)"
    if refused:
        msg = _public(refused[0]["detail"])[:90].replace("|", "/")
        text += f"<br><small>{msg}</small>"
    return text


def _examples(engine: str, cells: list[dict[str, Any]], limit: int = 3) -> list[str]:
    """The first non-agreeing cells, as table-safe one-liners."""
    out = []
    for c in cells:
        if c["status"] in ("disagree", "error", "unchecked") or (
            c["status"] == "refused" and c["group"] not in REFUSE
        ):
            cov = _cov_for(engine, c) or "UNEXPLAINED"
            out.append(
                f"{engine} `{c['native']}` {c['key']!r}: {c['status']} ({cov}) - "
                f"text `{c['text']!r}`, rules `{c['ref']!r}`"
                + (f" - {_public(c['detail'])[:140]}" if c["detail"] else "")
            )
            if len(out) >= limit:
                break
    return out


def _matrix(runs: dict[str, dict[str, Any]], engines: list[str]) -> list[str]:
    """The family x engine matrix for one scale."""
    head = "| Logical family | " + " | ".join(engines) + " |"
    lines = [head, "|" + "---|" * (len(engines) + 1)]
    for label, groups in FAMILIES:
        row = [label]
        for e in engines:
            cells = [c for c in runs[e]["cells"] if c["group"] in groups]
            row.append(
                _refusal_cell(cells)
                if groups == ("float_refuse",)
                else _cell_text(e, cells)
            )
        lines.append("| " + " | ".join(row) + " |")
    return lines


def _unknown_groups(runs: dict[str, dict[str, Any]]) -> set[str]:
    """Value groups loaded only into UNKNOWN-mapped columns."""
    groups = {c["group"] for r in runs.values() for c in r["cells"]}
    return {g for g in groups if g not in RULED}


def _unknown_section(runs: dict[str, dict[str, Any]], engines: list[str]) -> list[str]:
    """UNKNOWN-mapped natives: per engine, injectivity on the sample and texts."""
    lines = [
        (
            "Native types the dialect maps to UNKNOWN have no comparison rule: each "
            "engine compares them through its own text cast (escaped). Nothing "
            "guarantees two engines agree, and - the false-match risk - nothing "
            "guarantees one engine renders two different values differently. The "
            "sample checks the second on the values loaded: *collisions* are pairs "
            "of distinct values that render to the same text."
        ),
        "",
        "| Engine | Native type | Values | Distinct texts | Collisions | Example texts |",
        "|---|---|---|---|---|---|",
    ]
    for e in engines:
        by: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for c in runs[e]["cells"]:
            if c["logical"] == "unknown" and c["status"] in ("unruled", "error"):
                by[c["native"]].append(c)
        for native in sorted(by):
            cells = [c for c in by[native] if c["text"] is not None]
            texts: dict[str, list[str]] = defaultdict(list)
            for c in cells:
                texts[c["text"]].append(c["key"])
            coll = [ks for ks in texts.values() if len(ks) > 1]
            cov = COLLISIONS.get((e, native))
            coll_text = (
                "none"
                if not coll
                else f"**{len(coll)}** ({cov or 'UNEXPLAINED'}): "
                + "; ".join(" = ".join(f"`{k}`" for k in ks) for ks in coll[:2])
            )
            ex = ", ".join(f"`{c['text']!r}`" for c in cells[1:4])
            lines.append(
                f"| {e} | `{native}` | {len(by[native])} | {len(texts)} | {coll_text} | {ex} |"
            )
    lines += ["", "Not loaded by this suite (no measurement either way):", ""]
    for e in engines:
        nl = NOT_LOADED.get(e)
        if nl:
            lines.append(
                f"- **{e}** - mapped but not loaded: {nl['mapped']}. "
                f"UNKNOWN, not loaded: {nl['unknown']}."
            )
    return lines


def _cross_section(runs: dict[str, dict[str, Any]]) -> list[str]:
    """Cross-engine agreement: shared values, and every split by kind."""
    shared, splits = compare(runs)
    forms = [s for s in splits if s.family in FORM_DEPENDENT]
    splits = [s for s in splits if s.family not in FORM_DEPENDENT]
    ruled_same = [s for s in splits if s.family in RULED and s.same_logical]
    mapping = [s for s in splits if s.family in RULED and not s.same_logical]
    unknown = [s for s in splits if s.family not in RULED]
    lines = [
        f"{shared} logical values are held by two or more engines. Of those:",
        "",
        (
            f"- **{len(ruled_same)}** render differently under the same logical type "
            "(a rendering disagreement: false alarms at least, and a false-match risk)"
        ),
        (
            f"- **{len(mapping)}** render differently because the engines map the "
            "native type to different logical types (a type-mapping mismatch: "
            "always reported as a difference, never a false match)"
        ),
        (
            f"- **{len(unknown)}** are UNKNOWN-mapped values that render differently "
            "(no rule; expected)"
        ),
        (
            f"- **{len(forms)}** are JSON documents whose engines' normal forms "
            "differ - key order, duplicate keys, number spelling, escapes kept as "
            "written - each held to the rule for its own engine, expected to "
            "differ across engines, and warned about in every such run (a false "
            "alarm, never a false match)"
        ),
        "",
    ]
    if forms:
        lines += ["**JSON normal forms, as each engine holds the same document:**", ""]
        lines += [f"- {s.describe().replace('|', '/')[:400]}" for s in forms]
        lines.append("")
    for title, group in (
        ("Same logical type, different text", ruled_same),
        ("Type-mapping mismatches", mapping),
    ):
        if group:
            lines += [f"**{title}:**", ""]
            for s in group[:12]:
                cov = (
                    SPLITS.get(s.family, "UNEXPLAINED")
                    if group is ruled_same
                    else "mapping"
                )
                lines.append(f"- ({cov}) " + s.describe().replace("|", "/")[:400])
            if len(group) > 12:
                lines.append(f"- ... and {len(group) - 12} more")
            lines.append("")
    clash = collisions(runs, {cross_group(g) for g in _unknown_groups(runs)})
    lines += [
        (
            "**Collisions: one text, two different UNKNOWN-mapped values** (on one "
            "engine or across two - a false match wherever no rule decides):"
        ),
        "",
    ]
    lines += [f"- {c.describe()}" for c in clash] or ["- none"]
    lines.append("")
    if unknown:
        fams = Counter(s.family for s in unknown)
        lines.append(
            "UNKNOWN families that differ across engines: "
            + ", ".join(f"{f} ({n} values)" for f, n in sorted(fams.items()))
            + "."
        )
    return lines


def render(results: Path) -> str:
    """The whole document."""
    runs6 = load_runs(results, 6)
    runs2 = load_runs(results, 2)
    engines = _engines(runs6)
    if not engines:
        raise SystemExit(f"no saved runs under {results}; run tests/conformance first")
    total6 = sum(len(r["cells"]) for r in runs6.values())
    total2 = sum(len(r["cells"]) for r in runs2.values())
    ruled = [
        (e, c)
        for e in engines
        for c in runs6[e]["cells"]
        if c["status"] not in ("unruled", "unstorable")
    ]
    agree = sum(1 for _, c in ruled if c["status"] == "agree")
    distinct = {(cross_group(c["group"]), c["key"]) for _, c in ruled}
    unexplained = [
        (e, c)
        for e in engines
        for runs in (runs6, runs2)
        if e in runs
        for c in runs[e]["cells"]
        if (c["status"] in ("disagree", "error", "unchecked")) and _cov_for(e, c) is None
    ]
    findings_seen = sorted(
        {
            cov
            for e in engines
            for c in runs6[e]["cells"]
            if (cov := _cov_for(e, c)) and cov.startswith("COV")
        }
        | {cov for (e, n), cov in COLLISIONS.items() if e in engines}
    )
    out = [
        "# Canonical-text coverage map",
        "",
        (
            "Generated by `tests/conformance/report.py` from the runs saved by "
            "`tests/conformance/` - do not edit by hand. Each engine's hard values "
            "(`cov_values.py`) are loaded through driver parameter binding, rendered "
            "by the dialect's own `normalize`, and compared with "
            "`parity.reference` applied to the value read back (the rules, in "
            "Python), then compared across engines."
        ),
        "",
        "## Summary",
        "",
        "| Engine | Run (UTC) | Seconds | Cells at scale 6 | Cells at scale 2 |",
        "|---|---|---|---|---|",
    ]
    for e in engines:
        r2 = runs2.get(e, {"cells": []})
        out.append(
            f"| {e} | {runs6[e]['when'][:19]} | {runs6[e]['seconds']} | "
            f"{len(runs6[e]['cells'])} | {len(r2['cells'])} |"
        )
    out += [
        "",
        (
            f"- **{total6 + total2}** (native type, value, scale) cells measured: "
            f"{total6} at float scale 6, {total2} at scale 2."
        ),
        (
            f"- At scale 6, **{agree} of {len(ruled)}** ruled cells render exactly as "
            f"the rules say ({len(distinct)} distinct logical values); the rest are "
            "required refusals or the findings below."
        ),
        f"- **{len(unexplained)}** unexplained disagreements or errors"
        + (" - see the matrix." if unexplained else "."),
        (
            "- **Verified:** for every engine run and every ruled family with a "
            "measured cell below, `normalize` equals the rules on every value "
            "loaded, at scales 6 and 2, except where a COV finding says otherwise."
        ),
        (
            "- **Not verified:** a family marked with a dash (the engine has no such type, "
            "or none was loaded); every native type listed under *Not loaded*; "
            "UNKNOWN-mapped types (no rule exists - their section shows only "
            "collisions on the sample); other float scales; other collations and "
            "server settings than the ones each dialect pins. *JSON, engine normal "
            "forms* is verified per engine only: across engines those documents "
            "differ by design (see *Across engines*)."
        ),
        "",
        "## Findings",
        "",
    ]
    for cov in sorted(FINDINGS):
        seen = "" if cov in findings_seen else " *(kept fixed by its regression test)*"
        out.append(f"- **{cov}**{seen} - {FINDINGS[cov]}")
    out += ["", "## Matrix at float scale 6", ""]
    out += _matrix(runs6, engines)
    out += ["", "## Matrix at float scale 2", ""]
    out += _matrix(runs2, [e for e in engines if e in runs2])
    examples = [x for e in engines for x in _examples(e, runs6[e]["cells"], 2)]
    if examples:
        out += ["", "First non-agreeing cells per engine (scale 6):", ""]
        out += [f"- {x}" for x in examples]
    out += ["", "## Per native type (scale 6)", ""]
    out += [
        (
            "| Engine | Native type | Logical | Values | Agree | Disagree | Refused | "
            "Unchecked | Not storable |"
        ),
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for e in engines:
        by: dict[tuple[str, str], Counter] = defaultdict(Counter)
        for c in runs6[e]["cells"]:
            if c["status"] == "unruled":
                continue
            by[(c["native"], c["logical"])][c["status"]] += 1
        for (native, logical), st in sorted(by.items()):
            n = sum(st.values())
            out.append(
                f"| {e} | `{native}` | {logical} | {n} | {st['agree']} | "
                f"{st['disagree'] + st['error']} | {st['refused']} | {st['unchecked']} | "
                f"{st['unstorable']} |"
            )
    out += ["", "## Across engines (scale 6)", ""]
    out += _cross_section(runs6)
    out += ["", "## UNKNOWN-mapped types (scale 6)", ""]
    out += _unknown_section(runs6, engines)
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    """Write the map; print where."""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--results", type=Path, default=RESULTS)
    p.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "docs" / "coverage.md",
    )
    a = p.parse_args(argv)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(render(a.results), encoding="utf-8")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
