"""Hand-chosen semantic mutation harness for parity's test suite.

Not collected by pytest (no ``test_`` prefix). Each mutation is a literal
``(file, old, new)`` replacement - or several, applied together - that
reintroduces a bug class CLAUDE.md documents, or one that would cause a false
match. The product tree is never touched: every mutation is applied to a fresh
*copy* of ``src/`` in a scratch directory, and the suite runs against that copy
through ``PYTHONPATH`` (checked per run: ``parity.__file__`` must resolve into
the copy, or the run is recorded as an error rather than a kill).

    python tests/redteam/mutate.py                 # every mutation
    python tests/redteam/mutate.py E1 P3           # just these
    python tests/redteam/mutate.py --list
    python tests/redteam/mutate.py E1 -- tests/redteam   # extra pytest args

The caller supplies database URLs (``PARITY_TEST_PG``, ``PARITY_TEST_MSSQL``,
...) exactly as the normal suite expects them. ``MUTATE_WORK`` names the
scratch directory (default: a fresh temp dir) and ``MUTATE_OUT`` the JSON
results file. ``MUTATE_TESTS`` overrides the test selection for every mutation
(space-separated). BigQuery and Snowflake live suites skip without their URLs,
so their mutations are judged by the offline tests alone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "src")

ENGINE = "src/parity/engine.py"
BASE = "src/parity/dialects/base.py"
TYPES = "src/parity/types.py"
PG = "src/parity/dialects/postgres_dialect.py"
DUCK = "src/parity/dialects/duckdb_dialect.py"
MY = "src/parity/dialects/mysql_dialect.py"
MS = "src/parity/dialects/mssql_dialect.py"
BQ = "src/parity/dialects/bigquery_dialect.py"
SF = "src/parity/dialects/snowflake_dialect.py"

#: The existing suite only - the "before" score. The redteam kills are run
#: separately (``-- tests/redteam``) for the "after" score.
#: The docstring lint reads the *real* tree (not the mutated copy), so it can
#: never see a mutation - but it can fail on work in progress under tests/ and
#: masquerade as a kill. Deselected here; the normal suite still runs it.
T_SUITE = [
    "tests",
    "--ignore=tests/redteam",
    "--deselect=tests/test_encoding.py::test_every_function_has_a_docstring",
]


@dataclass
class Mutation:
    """One semantic bug to plant, and whether it could cause a false match."""

    id: str
    why: str
    edits: list[tuple[str, str, str]]
    tests: list[str] = field(default_factory=lambda: list(T_SUITE))
    #: Could this bug report two different tables as identical in real use?
    false_match: bool = True


_DESELECT = T_SUITE[2]

#: Dialect-only mutations run only the test files that exercise that dialect
#: (found by grepping tests/ for its fixtures and URLs) - a file that never
#: opens a MySQL connection cannot kill a MySQL mutation, and the full suite
#: takes ten minutes on a shared machine. Engine, base, types, PostgreSQL and
#: DuckDB mutations run everything, since nearly every file touches them.
_BY_FILE = {
    MY: [
        "test_mysql.py",
        "test_mysql_postgres.py",
        "test_decimal_range.py",
        "test_float_canonical.py",
        "test_cli.py",
        "test_encoding.py",
        "test_mssql_offline.py",
    ],
    MS: ["test_mssql_offline.py", "test_mssql.py", "test_encoding.py"],
    BQ: [
        "test_bigquery_offline.py",
        "test_bigquery.py",
        "test_decimal_range.py",
        "test_float_canonical.py",
        "test_engine.py",
        "test_mssql_offline.py",
        "test_encoding.py",
    ],
    SF: [
        "test_snowflake_offline.py",
        "test_snowflake.py",
        "test_engine.py",
        "test_float_canonical.py",
        "test_mssql_offline.py",
        "test_encoding.py",
    ],
}


def _tests_for(file: str) -> list[str]:
    """The test selection for a mutation of `file` (see `_BY_FILE`)."""
    if file not in _BY_FILE:
        return list(T_SUITE)
    return [f"tests/{name}" for name in _BY_FILE[file]] + [_DESELECT]


def m(
    id_: str,
    why: str,
    file: str,
    old: str,
    new: str,
    false_match: bool = True,
    more: tuple[tuple[str, str, str], ...] = (),
) -> Mutation:
    """Build a one-edit mutation (plus `more` edits applied with it)."""
    return Mutation(id_, why, [(file, old, new), *more], _tests_for(file), false_match)


# fmt: off
MUTATIONS: list[Mutation] = [
    # ------------------------------------------------------------ engine.py
    m("E1", "bucket_bounds: floor instead of ceiling division", ENGINE,
      "    b_lo = lo + -(-(i * span) // n)\n    b_hi = lo + -(-((i + 1) * span) // n)",
      "    b_lo = lo + (i * span) // n\n    b_hi = lo + ((i + 1) * span) // n"),
    m("E2", "bucket_bounds: bucket upper bound one short (skips a key)", ENGINE,
      "    b_hi = lo + -(-((i + 1) * span) // n)\n",
      "    b_hi = lo + -(-((i + 1) * span) // n) - (1 if i < n - 1 else 0)\n"),
    m("E3", "initial range exclusive of the max key (no +1)", ENGINE,
      "lo, hi = min(bounds), max(bounds) + 1", "lo, hi = min(bounds), max(bounds)"),
    m("E4", "threshold `<=` -> `<`", ENGINE,
      "if max(va[0], vb[0]) <= threshold or", "if max(va[0], vb[0]) < threshold or",
      false_match=False),
    m("E5", "bucket compare on row count only, ignoring checksum", ENGINE,
      "i for i in range(n) if cs_a.get(i, EMPTY) != cs_b.get(i, EMPTY)",
      "i for i in range(n) if cs_a.get(i, EMPTY)[0] != cs_b.get(i, EMPTY)[0]"),
    m("E6", "bucket compare on checksum only, ignoring count", ENGINE,
      "i for i in range(n) if cs_a.get(i, EMPTY) != cs_b.get(i, EMPTY)",
      "i for i in range(n) if cs_a.get(i, EMPTY)[1] != cs_b.get(i, EMPTY)[1]"),
    m("E7", "one-key table never compared", ENGINE,
      "                    if va != vb:\n                        compare(s_lo, s_hi, va, vb)\n",
      "                    if False:\n                        compare(s_lo, s_hi, va, vb)\n"),
    m("E8", "threshold uses min of both sides", ENGINE,
      "if max(va[0], vb[0]) <= threshold or", "if min(va[0], vb[0]) <= threshold or",
      false_match=False),
    m("E9", "only_in_b reported as only_in_a", ENGINE,
      'k, "only_in_b", names, {}, dict(zip(names, rb or (), strict=True))',
      'k, "only_in_a", names, {}, dict(zip(names, rb or (), strict=True))'),
    m("E10", "changed-column list inverted (x == y)", ENGINE,
      "changed = [n for n, x, y in zip(names, ra, rb, strict=True) if x != y]",
      "changed = [n for n, x, y in zip(names, ra, rb, strict=True) if x == y]"),
    m("E11", "row comparison skipped for rows present on both sides", ENGINE,
      "        elif ra != rb:\n", "        elif False:\n"),
    m("E12", "truncated only when more than one range unchecked", ENGINE,
      "    if unchecked:\n        # Partial", "    if unchecked > 1:\n        # Partial"),
    m("E13", "overshoot trim not counted as unchecked", ENGINE,
      "        unchecked += len(diffs) - max_diffs\n", "        pass\n"),
    m("E14", "limit_reached `>=` -> `>`", ENGINE,
      "return max_diffs is not None and len(diffs) >= max_diffs",
      "return max_diffs is not None and len(diffs) > max_diffs", false_match=False),
    m("E15", "column matching case-sensitive (no casefold)", ENGINE,
      "        fold = c.name.casefold()\n", "        fold = c.name\n"),
    m("E16", "last shared column silently dropped", ENGINE,
      "    shared = sorted(both - excluded)\n", "    shared = sorted(both - excluded)[:-1]\n"),
    m("E17", "duplicate-key refusal removed", ENGINE,
      "            if ks.has_duplicate_keys:\n", "            if False:\n"),
    m("E18", "NULL-key refusal removed", ENGINE,
      "            if ks.has_null_keys:\n", "            if False:\n"),
    m("E19", "a queued sub-range is occasionally dropped", ENGINE,
      "                s_lo, s_hi = queue.pop()\n",
      "                s_lo, s_hi = queue.pop()\n"
      "                if len(queue) % 7 == 3:\n                    queue.pop()\n"),
    m("E20", "--exclude ignored", ENGINE,
      "    excluded = {e.casefold() for e in exclude}\n", "    excluded = set()\n",
      false_match=False),
    m("E21", "-only-on-one-side warning dropped", ENGINE,
      '            warnings.append(f"not compared, present only on side {side}: {names}")\n',
      "            pass\n"),
    # ------------------------------------------------------------- types.py
    m("T1", "identical ignores truncated", TYPES,
      "            and not self.truncated\n", ""),
    m("T2", "duplicate check `!=` -> `<`", TYPES,
      "return comparable != self.distinct", "return comparable < self.distinct"),
    m("T3", "null_keys always 0", TYPES,
      "return 0 if self.non_null is None else self.rows - self.non_null", "return 0"),
    # -------------------------------------------------------------- base.py
    m("B1", "HASH_HEX_CHARS 15 -> 16", BASE, "HASH_HEX_CHARS = 15", "HASH_HEX_CHARS = 16"),
    m("B2", "HASH_HEX_CHARS 15 -> 14", BASE, "HASH_HEX_CHARS = 15", "HASH_HEX_CHARS = 14"),
    m("B3", "MAX_CONCAT_ARGS 64 -> 200 (flat concat_ws)", BASE,
      "MAX_CONCAT_ARGS = 64", "MAX_CONCAT_ARGS = 200", false_match=False),
    m("B4", "concat tree drops the last value of each group", BASE,
      "            self._concat(parts[i : i + MAX_CONCAT_ARGS])\n",
      "            self._concat(parts[i : i + MAX_CONCAT_ARGS - 1])\n"),
    m("B5", "NULL sentinel is the empty string", BASE,
      'NULL_SENTINEL = "\\\\N"', 'NULL_SENTINEL = ""'),
    m("B6", "checksum WHERE upper bound excludes hi-1", BASE,
      "and {k} <= {self.int_literal(hi - 1)} ", "and {k} < {self.int_literal(hi - 1)} "),
    m("B7", "checksum WHERE lower bound exclusive", BASE,
      "where {k} >= {self.int_literal(lo)} and", "where {k} > {self.int_literal(lo)} and"),
    m("B8", "key not folded into the row checksum", BASE,
      # `or ["''"]`: with no comparable columns the key was the only part, and
      # an empty concat_ws is a syntax error (row_text's constant fallback).
      "            [*self._key_parts(key), *(self.normalize(c) for c in columns)]\n",
      "            [*(self.normalize(c) for c in columns)] or [\"''\"]\n"),
    m("B9", "fetch_range upper bound excludes hi-1", BASE,
      "and {bucket} <= {hi - 1} order by", "and {bucket} < {hi - 1} order by"),
    m("B10", "fetch_range duplicate-key guard removed", BASE,
      "            if rk in out:\n", "            if False:\n"),
    m("B11", "key_stats counts NULL keys as non-null", BASE,
      'f"{non_null_sql}, {self.count_distinct_key(key, identity)} "',
      'f"{self.count()}, {self.count_distinct_key(key, identity)} "'),
    m("B12", "hashed key identity is the hash, not the text", BASE,
      "            return self.quote(key.columns[0].name)\n"
      "        return self._concat(self._key_parts(key))\n",
      "            return self.quote(key.columns[0].name)\n"
      "        return self.hash_expr(self._concat(self._key_parts(key)))\n"),
    m("B13", "map_type: timestamp matched exactly, not by prefix", BASE,
      'if t.startswith(("timestamp", "datetime")):', 'if t in ("timestamp", "datetime"):',
      false_match=False),
    m("B14", "require_matching_scales disabled", BASE,
      "    if a.float_scale != b.float_scale:\n", "    if False:\n", false_match=False),
    m("B15", "offset widened after subtraction: wide_int(k - lo)", BASE,
      'offset = f"({self.wide_int(k)} - ({self.int_literal(lo)}))"',
      "offset = f\"({self.wide_int(f'{k} - ({self.int_literal(lo)})')})\"",
      false_match=False),
    m("B16", "span evaluated in SQL as hi - lo", BASE,
      'f"({self.int_literal(hi - lo)})"',
      'f"({self.int_literal(hi)} - {self.int_literal(lo)})"', false_match=False),
    m("B17", "canonical float spec rounds half-even", BASE,
      "rounding=ROUND_HALF_UP, context=ctx", "rounding=ROUND_HALF_EVEN, context=ctx",
      more=((BASE, "from decimal import ROUND_HALF_UP, Context, Decimal",
             "from decimal import ROUND_HALF_EVEN, Context, Decimal"),),
      false_match=False),
    m("B18", "map_type: numeric not DECIMAL", BASE,
      'if t in {"decimal", "numeric"}:', 'if t in {"decimal"}:', false_match=False),
    m("B19", "separator chr(31) -> chr(44) (comma)", BASE,
      '    "chr(31)"  # default', '    "chr(44)"  # default'),
    m("B20", "fetch_range returns only the first column", BASE,
      '        exprs = "".join(", " + self.fetch_text(self.normalize(c)) for c in columns)\n',
      '        exprs = "".join(", " + self.fetch_text(self.normalize(c)) for c in columns[:1])\n'
      '        exprs += "".join(", \'\'" for c in columns[1:])\n'),
    # ------------------------------------------------------------ postgres
    m("P1", "PG boolean `else` swallows NULL", PG,
      "expr = f\"case when {c} then 'true' when not {c} then 'false' end\"",
      "expr = f\"case when {c} then 'true' else 'false' end\""),
    m("P2", "PG no coalesce (NULL drops out of concat_ws)", PG,
      'return f"coalesce({expr}, {self.null_sentinel_sql()})"', 'return f"{expr}"'),
    m("P3", "PG hash prefix 16 hex chars (bit(60) truncates it back)", PG,
      "substr(md5({text_expr}), 1, {HASH_HEX_CHARS})", "substr(md5({text_expr}), 1, 16)",
      false_match=False),
    m("P4", "PG UTC pin removed", PG,
      "    (\"TimeZone\", \"'UTC'\"),\n", ""),
    m("P5", "PG extra_float_digits pin removed", PG,
      '    ("extra_float_digits", "1"),\n', ""),
    m("P6", "PG REPEATABLE READ removed", PG,
      "self._conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ", "pass"),
    m("P7", "PG int_div uses numeric `/`", PG,
      'return f"div(({numerator})::numeric, ({denominator})::numeric)"',
      'return f"(({numerator})::numeric / ({denominator})::numeric)"'),
    m("P8", "PG wide_int no widening", PG,
      'return f"(({expr})::numeric)"', 'return f"({expr})"', false_match=False),
    m("P9", "PG sum not cast to numeric", PG,
      'return f"coalesce(sum(({expr})::numeric), 0)"', 'return f"coalesce(sum({expr}), 0)"',
      false_match=False),
    m("P10", "PG NaN detected with c <> c", PG,
      "f\"when {c} = 'NaN'::float8 then 'NaN' \"", "f\"when {c} <> {c} then 'NaN' \""),
    m("P11", "PG DECIMAL truncates instead of rounding", PG,
      'expr = f"cast(round(({c})::numeric, {self.float_scale}) as text)"',
      'expr = f"cast(trunc(({c})::numeric, {self.float_scale}) as text)"'),
    m("P12", "PG timestamp at milliseconds", PG,
      'expr = self._datetime_text(c, "-MM-DD HH24:MI:SS.US")',
      'expr = self._datetime_text(c, "-MM-DD HH24:MI:SS.MS")'),
    m("P13", "PG FLOAT not widened to float8 first", PG,
      'c = f"({c})::float8"', "pass"),
    m("P14", "PG -Infinity token rendered as Infinity", PG,
      "f\"when {c} = '-Infinity'::float8 then '-Infinity' \"",
      "f\"when {c} = '-Infinity'::float8 then 'Infinity' \""),
    # -------------------------------------------------------------- duckdb
    m("D1", "DuckDB boolean `else` swallows NULL", DUCK,
      "expr = f\"case when {c} then 'true' when not {c} then 'false' end\"",
      "expr = f\"case when {c} then 'true' else 'false' end\""),
    m("D2", "DuckDB UTC pin removed", DUCK,
      "self._conn.execute(\"set TimeZone='UTC'\")", "pass"),
    m("D3", "DuckDB int_div `/` (double)", DUCK,
      'return f"(({numerator}) // ({denominator}))"',
      'return f"(({numerator}) / ({denominator}))"'),
    m("D4", "DuckDB sum not cast to decimal", DUCK,
      'return f"coalesce(sum(cast(({expr}) as decimal(38,0))), 0)"',
      'return f"coalesce(sum({expr}), 0)"', false_match=False),
    m("D5", "DuckDB wide_int no widening", DUCK,
      'return f"cast(({expr}) as hugeint)"', 'return f"({expr})"', false_match=False),
    m("D6", "DuckDB tiny-float short-circuit removed", DUCK,
      "f\"when abs({c}) < cast('{float_tiny_sql(s)}' as double) then '{zero}' \"", '""',
      false_match=False),
    m("D7", "DuckDB timestamp at milliseconds", DUCK,
      "expr = f\"strftime({c}, '%Y-%m-%d %H:%M:%S.%f')\"",
      "expr = f\"strftime({c}, '%Y-%m-%d %H:%M:%S.%g000')\""),
    m("D8", "DuckDB DECIMAL at scale-3", DUCK,
      'expr = f"cast(cast({c} as decimal(38,{self.float_scale})) as varchar)"',
      'expr = f"cast(cast({c} as decimal(38,{self.float_scale - 3})) as varchar)"'),
    m("D9", "DuckDB NaN renders as Infinity", DUCK,
      "f\"when isnan({c}) then 'NaN' \"", "f\"when isnan({c}) then 'Infinity' \""),
    m("D10", "DuckDB -Infinity renders as Infinity", DUCK,
      "f\"else '-Infinity' end) \"", "f\"else 'Infinity' end) \""),
    m("D11", "DuckDB float4 not widened to double", DUCK,
      'c = f"cast({c} as double)"', "pass"),
    m("D12", "DuckDB exact-integer band rendered via decimal(38,s)", DUCK,
      "f\"then cast(cast({c} as hugeint) as varchar) || '{frac}' \"",
      "f\"then cast(cast({c} as decimal(38,{s})) as varchar) \""),
    # --------------------------------------------------------------- mysql
    m("M1", "MySQL boolean `else` swallows NULL", MY,
      "expr = f\"case when {c} then 'true' when not {c} then 'false' end\"",
      "expr = f\"case when {c} then 'true' else 'false' end\""),
    m("M2", "MySQL DECIMAL out-of-range guard removed (clamps)", MY,
      'f"case when abs({c}) > {widest} "', 'f"case when 1 = 0 "'),
    m("M3", "MySQL DECIMAL cast back to decimal(38,s)", MY,
      'f"else cast(cast({c} as decimal({MYSQL_MAX_DECIMAL_DIGITS},{s})) "',
      'f"else cast(cast({c} as decimal(38,{s})) "'),
    m("M4", "MySQL UTC pin removed", MY,
      "cur.execute(\"set session time_zone = '+00:00'\")", "pass"),
    m("M5", "MySQL NULL sentinel empty", MY,
      'return "convert(0x5c4e using utf8mb4)"', "return \"''\""),
    m("M6", "MySQL int_div `/`", MY,
      'return f"(({numerator}) div ({denominator}))"',
      'return f"(({numerator}) / ({denominator}))"'),
    m("M7", "MySQL huge FLOAT rendered instead of raising", MY,
      'f"when {c} is not null then cast(uuid_to_bin(concat("\n'
      "                f\"'{FLOAT_RANGE_MARKER}', cast({c} as char), \"\n"
      '                f"convert(0x{float_range_where(column).encode().hex()} using utf8mb4)"\n'
      '                f")) as char) end"',
      'f"when {c} is not null then cast({c} as char) end"', false_match=False),
    m("M8", "MySQL separator is a comma", MY,
      'return "char(31 using utf8mb4)"', "return \"','\""),
    m("M9", "MySQL timestamp without microseconds", MY,
      "expr = f\"date_format({c}, '%Y-%m-%d %H:%i:%s.%f')\"",
      "expr = f\"date_format({c}, '%Y-%m-%d %H:%i:%s.000000')\""),
    m("M10", "MySQL hash prefix 16", MY,
      'f"cast(conv(substr(md5({text_expr}), 1, {HASH_HEX_CHARS}), 16, 10) "',
      'f"cast(conv(substr(md5({text_expr}), 1, 16), 16, 10) "'),
    m("M11", "MySQL REPEATABLE READ belt removed + autocommit on", MY,
      "        self._conn.autocommit = False  # type: ignore[union-attr]\n", "        pass\n"),
    # -------------------------------------------------------------- mssql
    m("S1", "MSSQL boolean `else` swallows NULL", MS,
      "expr = f\"case when {c} = 1 then N'true' when {c} = 0 then N'false' end\"",
      "expr = f\"case when {c} = 1 then N'true' else N'false' end\""),
    m("S2", "MSSQL strings not widened to nvarchar(max)", MS,
      "        elif t is LogicalType.STRING:\n            expr = f\"cast({c} as {text})\"",
      "        elif t is LogicalType.STRING:\n"
      "            expr = f\"cast({c} as nvarchar(4000))\""),
    m("S3", "MSSQL every value nvarchar(4000)", MS,
      '        t = column.logical_type\n        text = "nvarchar(max)"\n',
      '        t = column.logical_type\n        text = "nvarchar(4000)"\n'),
    m("S4", "MSSQL int_div plain `/`", MS,
      'return f"cast(({n} - {n} % {d}) / {d} as bigint)"',
      'return f"cast({n} / {d} as bigint)"'),
    m("S5", "MSSQL distinct keys counted on collated text", MS,
      "return f\"count_big(distinct hashbytes('SHA2_256', {self._utf8(identity)}))\"",
      'return f"count_big(distinct {identity})"', false_match=False),
    m("S6", "MSSQL char rtrim removed", MS,
      'expr = f"rtrim(cast({c} as {text}))"', 'expr = f"cast({c} as {text})"',
      false_match=False),
    m("S7", "MSSQL uniqueidentifier not lower-cased", MS,
      'expr = f"lower(convert({text}, {c}))"', 'expr = f"convert({text}, {c})"',
      false_match=False),
    m("S8", "MSSQL datetime2 at milliseconds", MS,
      'return f"left(convert(nvarchar(max), cast({c} as datetime2(7)), 121), 26)"',
      "return f\"left(convert(nvarchar(max), cast({c} as datetime2(7)), 121), 23)"
      " + N'000'\""),
    m("S9", "MSSQL snapshot never set (silent READ COMMITTED)", MS,
      '+ ("snapshot" if self.snapshot else "read committed")', '+ "read committed"'),
    m("S10", "MSSQL UTF-8 collation dropped before MD5", MS,
      'f"collate {UTF8_COLLATION} as varchar(max))"', 'f"as varchar(max))"',
      false_match=False),
    m("S11", "MSSQL non-SELECT refusal removed", MS,
      "        if not is_single_select(sql):\n", "        if False:\n",
      false_match=False),
    m("S12", "MSSQL hash prefix 16", MS,
      'f"left(convert(varchar(32), {digest}, 2), {HASH_HEX_CHARS}), 2))"',
      'f"left(convert(varchar(32), {digest}, 2), 16), 2))"'),
    m("S13", "MSSQL datetimeoffset not moved to UTC", MS,
      "c = f\"switchoffset({c}, '+00:00')\"", "pass", false_match=False),
    m("S14", "MSSQL float tie goes to odd", MS,
      'f"and floor(abs({t}) / nullif(abs({r} - {t}), 0)) % 2 = 0 "',
      'f"and floor(abs({t}) / nullif(abs({r} - {t}), 0)) % 2 = 1 "', false_match=False),
    m("S15", "MSSQL lone concat part wrapped in CONCAT_WS", MS,
      "        if len(parts) == 1:\n            return parts[0]\n", "", false_match=False),
    m("S16", "MSSQL datetime rendered through datetime2", MS,
      "            return f\"convert(nvarchar(max), {c}, 121) + N'000'\"\n", "",
      false_match=False),
    m("S17", "MSSQL DECIMAL at scale-3", MS,
      'expr = f"convert({text}, cast({c} as decimal(38,{self.float_scale})))"',
      'expr = f"convert({text}, cast({c} as decimal(38,{self.float_scale - 3})))"'),
    m("S18", "MSSQL NULL sentinel empty", MS,
      "return f\"N'{NULL_SENTINEL}'\"", "return \"N''\""),
    # ----------------------------------------------- bigquery (offline only)
    m("G1", "BigQuery boolean `else`", BQ,
      "expr = f\"case when {c} then 'true' when not {c} then 'false' end\"",
      "expr = f\"case when {c} then 'true' else 'false' end\""),
    m("G2", "BigQuery %t NULL guard removed ('NULL' text)", BQ,
      "expr = self.escape_text(f\"if({c} is null, null, format('%t', {c}))\")",
      "expr = self.escape_text(f\"format('%t', {c})\")"),
    m("G3", "BigQuery hash prefix 16", BQ,
      'f"{HASH_HEX_CHARS})) as int64)"', 'f"16)) as int64)"'),
    m("G4", "BigQuery int_literal plain digits", BQ,
      "return f\"bignumeric '{value}'\"", "return str(value)", false_match=False),
    m("G5", "BigQuery DECIMAL without ROUND", BQ,
      'expr = f"format({fmt}, round({num}, {s}))"', 'expr = f"format({fmt}, {num})"',
      false_match=False),
    m("G6", "BigQuery naive datetime rendered as UTC timestamp", BQ,
      "            if column.tz_aware is False:\n", "            if False:\n",
      false_match=False),
    m("G7", "BigQuery NULL sentinel empty", BQ,
      "return \"concat(chr(92), 'N')\"", "return \"''\""),
    # ---------------------------------------------- snowflake (offline only)
    m("F1", "Snowflake hash /8 instead of /16", SF,
      'return f"floor(md5_number_upper64({text_expr}) / 16)"',
      'return f"floor(md5_number_upper64({text_expr}) / 8)"'),
    m("F2", "Snowflake boolean `else`", SF,
      "expr = f\"case when {c} then 'true' when not {c} then 'false' end\"",
      "expr = f\"case when {c} then 'true' else 'false' end\""),
    # The exact `(n - mod(n, d)) / d` replaced floor(n / d), whose quotient
    # Snowflake rounds to six places first; the mutant reverts to that form.
    m("F3", "Snowflake int_div back to floor of a rounded quotient", SF,
      'return f"cast(({n} - mod({n}, {d})) / {d} as number(38,0))"',
      'return f"floor({n} / {d})"'),
    m("F4", "Snowflake NUMBER scale 0 not INTEGER", SF,
      "return LogicalType.INTEGER if scale == 0 else LogicalType.DECIMAL",
      "return LogicalType.DECIMAL", false_match=False),
    m("F5", "Snowflake UTC pin removed", SF,
      "cur.execute(\"alter session set timezone = 'UTC'\")", "pass"),
    m("F6", "Snowflake NULL sentinel empty", SF,
      "return \"(chr(92) || 'N')\"", "return \"''\""),
    # ------------------------------------------- second batch (resumed run)
    m("X1", "PG + DuckDB checksum XOR instead of SUM (cancels pairs)", PG,
      'return f"coalesce(sum(({expr})::numeric), 0)"', 'return f"coalesce(bit_xor({expr}), 0)"',
      more=((DUCK, 'return f"coalesce(sum(cast(({expr}) as decimal(38,0))), 0)"',
             'return f"coalesce(bit_xor({expr}), 0)"'),)),
    m("X2", "PG empty bucket sums to NULL (coalesce dropped)", PG,
      'return f"coalesce(sum(({expr})::numeric), 0)"', 'return f"sum(({expr})::numeric)"',
      false_match=False),
    m("D13", "DuckDB no coalesce (NULL drops out of concat_ws)", DUCK,
      'return f"coalesce({expr}, {self.null_sentinel_sql()})"', 'return f"{expr}"'),
    m("D14", "DuckDB exact-integer band drops the fraction suffix", DUCK,
      "f\"then cast(cast({c} as hugeint) as varchar) || '{frac}' \"",
      "f\"then cast(cast({c} as hugeint) as varchar) \"", false_match=False),
    m("D15", "DuckDB huge-FLOAT refusal replaced by a constant", DUCK,
      "f\"when {c} is not null then error('{FLOAT_OUT_OF_RANGE_MESSAGE}' || cast({c} as varchar) \"\n"
      "                f\"|| {sql_literal(float_range_where(column))}) end\"",
      "f\"when {c} is not null then 'huge' end\""),
    m("P15", "PG exact-integer band removed (shortest repr up to 2^116)", PG,
      'f"when abs({c}) < {two53} "', "f\"when abs({c}) < '{FLOAT_MAX_MAGNITUDE_SQL}'::float8 \"",
      false_match=False),
    m("P16", "PG huge-FLOAT refusal replaced by a constant", PG,
      "f\"when {c} is not null then cast(cast('{FLOAT_OUT_OF_RANGE_MESSAGE}' || cast({c} as text) \"\n"
      "                f\"|| {sql_literal(float_range_where(column))} as float8) as text) end\"",
      "f\"when {c} is not null then 'huge' end\""),
    m("M12", "MySQL no coalesce (NULL drops out of concat_ws)", MY,
      'return f"coalesce({expr}, {self.null_sentinel_sql()})"', 'return f"{expr}"'),
    m("M13", "MySQL exact-integer band removed (decimal(38,s) up to 2^116)", MY,
      'f"case when abs({c}) < {FLOAT_EXACT_INT_FROM} "',
      "f\"case when abs({c}) < cast('{FLOAT_MAX_MAGNITUDE_SQL}' as double) \""),
    m("M14", "MySQL sum not widened (decimal(65,0) -> signed)", MY,
      'return f"coalesce(sum(cast(({expr}) as decimal(65,0))), 0)"',
      'return f"coalesce(sum({expr}), 0)"', false_match=False),
    m("S19", "MSSQL no coalesce (NULL drops out of concat_ws)", MS,
      'return f"coalesce({expr}, {self.null_sentinel_sql()})"', 'return f"{expr}"'),
    m("S20", "MSSQL shortest repr skips the 16-digit candidate", MS,
      'f"when cast({c16} as float) = {x} then {c16} else {c17} end)"',
      'f"when 1 = 0 then {c16} else {c17} end)"', false_match=False),
    m("S21", "MSSQL exact-integer band drops the fraction suffix", MS,
      "f\"then convert(nvarchar(max), cast({x} as decimal(38,0))) + N'{frac}' \"",
      "f\"then convert(nvarchar(max), cast({x} as decimal(38,0))) \"", false_match=False),
    m("S22", "MSSQL snapshot rollback replaced by commit", MS,
      "self._conn.rollback()", "self._conn.commit()", false_match=False),
]
# fmt: on


def apply(mutation: Mutation, dest: str) -> None:
    """Copy src/ to dest and apply every edit, refusing ambiguous ones."""
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(SRC, dest, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    for rel, old, new in mutation.edits:
        path = os.path.join(dest, os.path.relpath(os.path.join(ROOT, rel), SRC))
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        hits = text.count(old)
        if hits != 1:
            raise SystemExit(
                f"{mutation.id}: pattern found {hits} times in {rel}: {old!r}"
            )
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace(old, new))


#: Fixture names the suite shares with *every* other checkout of this repo. On a
#: machine where several people (or agents) run the suite at once against the
#: same PostgreSQL and MySQL, two runs of `test_integration.py` deadlock: one
#: holds its REPEATABLE READ snapshot on `parity_test_it` while the other's
#: `drop schema` queues behind it and the first's next statement queues behind
#: the drop - a client-side cycle the server cannot detect. So the harness runs
#: an isolated *copy* of the repo whose fixtures carry an `rt_mut_` prefix.
#: Only names change; the tests' logic is byte-for-byte the suite's.
_ISOLATE = [
    ("tests/conftest.py", r'PG_SCHEMA = "parity_test"', 'PG_SCHEMA = "rt_mut_pt"'),
    (
        "tests/test_fuzz_encoding.py",
        r'FUZZ_SCHEMA = "parity_fuzz"',
        'FUZZ_SCHEMA = "rt_mut_fuzz"',
    ),
    ("tests/test_mysql.py", r"\b(orders|flags)\b", r"rt_mut_\1"),
    ("tests/test_mysql_postgres.py", r"\b(xeng(?:_p)?)\b", r"rt_mut_\1"),
    ("tests/test_decimal_range.py", r"\b(dec_range(?:_b)?)\b", r"rt_mut_\1"),
    ("tests/test_float_canonical.py", r"\b(float_canon_[ab])\b", r"rt_mut_\1"),
]

#: Wall-clock cap per shard. A shard that hits it is "timeout" - inconclusive,
#: never counted as a kill.
SHARD_TIMEOUT = float(os.environ.get("MUTATE_SHARD_TIMEOUT", "1500"))


def prepare_repo(work: str) -> str:
    """Copy the repository (minus VCS and caches) into `work/repo`, isolated."""
    import re

    repo = os.path.join(work, "repo")
    if os.path.exists(repo):
        shutil.rmtree(repo)
    shutil.copytree(
        ROOT,
        repo,
        ignore=shutil.ignore_patterns(".git", ".hypothesis", "__pycache__", "*.egg-info"),
    )
    if os.environ.get("MUTATE_ISOLATE", "1") != "0":
        for rel, pattern, repl in _ISOLATE:
            path = os.path.join(repo, rel)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            new, hits = re.subn(pattern, repl, text)
            if not hits:
                raise SystemExit(f"isolation pattern {pattern!r} not found in {rel}")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new)
    # Hypothesis' 200 ms per-example deadline fails the *unmutated* suite on a
    # loaded machine (seen: DeadlineExceeded in test_properties), which would
    # read as a kill. Timing is not what a mutation run measures; examples,
    # seeds and assertions are untouched.
    with open(os.path.join(repo, "tests", "conftest.py"), "a", encoding="utf-8") as fh:
        fh.write(
            "\n\nfrom hypothesis import settings as _rt_settings\n\n"
            '_rt_settings.register_profile("rt_mut", deadline=None)\n'
            '_rt_settings.load_profile("rt_mut")\n'
        )
    return repo


def _pytest(
    repo: str, env: dict[str, str], args: list[str], log: Any
) -> subprocess.Popen:
    """Start one pytest process in the isolated repo copy."""
    return subprocess.Popen(  # noqa: S603 - argv is ours, not user input
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfE", *args],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=repo,
    )


def run(mutation: Mutation, work: str, extra: list[str]) -> dict[str, object]:
    """Apply one mutation to the isolated copy and run its tests against it."""
    repo = os.path.join(work, "repo")
    dest = os.path.join(repo, "src")
    apply(mutation, dest)
    env = dict(os.environ)
    env["PYTHONPATH"] = dest + os.pathsep + os.path.join(repo, "tests")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    probe = subprocess.run(
        [sys.executable, "-c", "import parity; print(parity.__file__)"],
        env=env,
        capture_output=True,
        text=True,
        cwd=repo,
        check=False,
    )
    if not probe.stdout.strip().startswith(dest):
        return {
            "id": mutation.id,
            "status": "error",
            "killer": probe.stdout + probe.stderr,
        }
    result = _run_shards(mutation, work, repo, env, extra)
    if result["status"] == "killed" and result.get("nodes"):
        # Confirm the kill: the first failing test alone must fail again. A
        # kill that does not reproduce is a flake (a shared-server hiccup),
        # not evidence, and is reported as such.
        node = str(result["nodes"][0])  # type: ignore[index]
        with open(os.path.join(work, "confirm.log"), "w+", encoding="utf-8") as log:
            code = _pytest(repo, env, [node], log).wait()
        if code == 0:
            result["status"] = "flaky-kill"
    return result


def _run_shards(
    mutation: Mutation, work: str, repo: str, env: dict[str, str], extra: list[str]
) -> dict[str, object]:
    """Run the mutation's selection as concurrent shards; first failure wins."""
    tests = os.environ.get("MUTATE_TESTS", "").split() or mutation.tests
    started = time.time()
    procs = []
    logs = []
    for i, shard in enumerate(_shards(tests)):
        log = open(os.path.join(work, f"shard{i}.log"), "w+", encoding="utf-8")
        logs.append(log)
        procs.append(_pytest(repo, env, ["-x", *shard, *extra], log))
    # First failing shard decides; the rest are stopped, since a kill is a kill.
    codes: list[int | None] = [None] * len(procs)
    timed_out = False
    while None in codes:
        for i, p in enumerate(procs):
            if codes[i] is None:
                codes[i] = p.poll()
        if any(c not in (None, 0, 5) for c in codes):
            break
        if time.time() - started > SHARD_TIMEOUT:
            timed_out = True
            break
        time.sleep(0.5)
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait()
    lines: list[str] = []
    failed: list[str] = []
    for i, log in enumerate(logs):
        log.seek(0)
        out = [ln for ln in log.read().splitlines() if ln.strip()]
        log.close()
        lines += out
        if codes[i] not in (None, 0, 5):
            failed += [ln for ln in out if ln.startswith(("FAILED", "ERROR"))] or out[-3:]
    real = [c for c in codes if c is not None]
    if any(c not in (0, 5) for c in real):
        status = "killed"
    elif timed_out:
        status = "timeout"
    elif all(c == 5 for c in real):
        status = "no-tests"
    else:
        status = "survived"
    nodes = [ln.split()[1] for ln in failed if ln.startswith(("FAILED ", "ERROR "))]
    return {
        "id": mutation.id,
        "why": mutation.why,
        "status": status,
        "false_match": mutation.false_match,
        "seconds": round(time.time() - started, 1),
        "killer": " | ".join(failed[:2]) if failed else (lines[-1] if lines else ""),
        "nodes": nodes,
    }


#: Slowest files first, so they start in separate shards. Measured roughly.
_HEAVY = [
    "test_identical.py",
    "test_properties.py",
    "test_fuzz_encoding.py",
    "test_identical_live.py",
    "test_mssql.py",
    "test_float_canonical.py",
    "test_encoding.py",
    "test_integration.py",
]
SHARDS = int(os.environ.get("MUTATE_SHARDS", "4"))


def _shards(tests: list[str]) -> list[list[str]]:
    """Split a selection into per-file shards that can run concurrently.

    Every live test file owns its own schema or table names, so files do not
    collide; tests *within* a file share fixtures and stay together.
    """
    opts = [t for t in tests if t.startswith("-")]
    paths = [t for t in tests if not t.startswith("-")]
    if paths == ["tests"]:
        ignored = {
            o.split("=", 1)[1].rstrip("/") for o in opts if o.startswith("--ignore=")
        }
        paths = sorted(
            f"tests/{f}"
            for f in os.listdir(os.path.join(ROOT, "tests"))
            if f.startswith("test_") and f.endswith(".py")
        )
        paths = [p for p in paths if not any(p.startswith(i) for i in ignored)]
        opts = [o for o in opts if not o.startswith("--ignore=")]
    if len(paths) <= 1:
        return [paths + opts]
    rank = {f"tests/{n}": i for i, n in enumerate(_HEAVY)}
    paths.sort(key=lambda p: rank.get(p, len(rank)))
    shards: list[list[str]] = [[] for _ in range(min(SHARDS, len(paths)))]
    for i, p in enumerate(paths):
        shards[i % len(shards)].append(p)
    out = []
    for s in shards:
        names = set(s)
        # Keep a deselect only in the shard that holds its file.
        own = [
            o
            for o in opts
            if not o.startswith("--deselect=")
            or o.split("=", 1)[1].split("::")[0] in names
        ]
        out.append(s + own)
    return out


def main(argv: list[str]) -> int:
    """Run the chosen mutations and write a JSON report."""
    if "--list" in argv:
        for mu in MUTATIONS:
            print(f"{mu.id:5} {mu.why}")
        return 0
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1 :]
    wanted = set(argv)
    # BASELINE is the unmutated tree through the same machinery: it must
    # survive, or the isolated copy itself is broken and no kill means anything.
    baseline = Mutation("BASELINE", "no mutation", [], list(T_SUITE), False)
    chosen = [mu for mu in [baseline, *MUTATIONS] if mu.id in wanted or not wanted]
    if not wanted:
        chosen = chosen[1:]
    work = os.environ.get("MUTATE_WORK") or tempfile.mkdtemp(prefix="rt_mut_")
    os.makedirs(work, exist_ok=True)
    prepare_repo(work)
    out = os.environ.get("MUTATE_OUT", os.path.join(work, "results.json"))
    results: list[dict[str, object]] = []
    if os.path.exists(out):
        with open(out, encoding="utf-8") as fh:
            ids = {c.id for c in chosen}
            results = [r for r in json.load(fh) if r["id"] not in ids]
    for mu in chosen:
        r = run(mu, work, extra)
        results.append(r)
        print(
            f"{r['id']!s:5} {r['status']!s:9} {r.get('seconds', '')}s  "
            f"{str(r.get('killer', ''))[:120]}",
            flush=True,
        )
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=1)
    shutil.rmtree(os.path.join(work, "src"), ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
