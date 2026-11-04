"""Round H: cross-engine differential fuzzing, wider than `fuzz_harness`.

Builds on `fuzz_harness` (same oracle, same writers) and adds what round E's
generator held fixed:

- every engine pair *including same-engine pairs* (PostgreSQL to PostgreSQL,
  SQL Server to SQL Server, ...), and Snowflake and BigQuery against DuckDB;
- 1-40 columns of every kind the rules cover, TIME, BINARY, UUID and JSON
  included; keys of every exact kind - integer, DECIMAL, text, date,
  timestamp, uuid, binary - alone and composite;
- random `--where` predicates (per side, in that engine's SQL; the oracle
  reads each side through the *same* predicate with the raw driver) and
  random `--map` renames (renamed keys and columns, two columns' names
  swapped, case changes);
- random bisection factor, threshold, `max_diffs` and `verify_sample`, and
  the sample check's own verdict: a case is only OK when the independent
  check raised no failure and nothing was left unexplained.

    python tests/redteam/fuzz_cross.py --minutes 30 --pairs postgres-duckdb

prints a tally per pair and every disagreement, shrunk, with its seed.
Tables are named `rth_x_*` and dropped after each case.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import os
import random
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]

import fuzz_harness as fh  # noqa: E402
from fuzz_harness import Col  # noqa: E402

from parity.dialects.base import get_dialect  # noqa: E402
from parity.engine import diff  # noqa: E402

LOCAL = ("postgres", "duckdb", "mysql", "mssql")
JSON_OK = {"postgres", "duckdb", "mysql"}
#: Kinds each engine can store. MySQL has no BOOLEAN (tinyint(1) reads as an
#: integer, a documented type difference, not a comparison).
NO_KIND = {
    "mysql": {"bool"},
    "bigquery": {"real", "json"},
    "snowflake": {"json", "real"},
    "mssql": {"json"},
}


# ------------------------------------------------------------- Snowflake


class Snowflake(fh.Engine):
    """Writer for Snowflake through the tool's own connection (it is the
    only way the URL is parsed); every write is a parameter-bound INSERT."""

    name = "snowflake"

    def __init__(self, url: str) -> None:
        """Connect the writer."""
        super().__init__(url)
        self.d = get_dialect(url, side="W")
        self.conn = self.d._conn

    def exec(self, sql: str, params: Any = None) -> None:
        """Run one statement."""
        cur = self.conn.cursor()
        try:
            cur.execute(sql, params)
        finally:
            cur.close()

    def rows(self, sql: str) -> list[tuple]:
        """Run a query, every row as a tuple."""
        cur = self.conn.cursor()
        try:
            cur.execute(sql)
            return [tuple(r) for r in cur.fetchall()]
        finally:
            cur.close()

    def load(
        self, table: str, names: list[str], cols: list[Col], rows: list[tuple]
    ) -> None:
        """Multi-row INSERT ... SELECT over bound text, cast per column."""
        if not rows:
            return

        def cast(i: int, c: Col) -> str:
            """The SELECT-list cast of bound parameter `i` to column `c`'s Snowflake type."""
            t = fh.ddl_type("snowflake", c, False)
            if c.kind == "binary":
                return f"to_binary(${i}::varchar, 'HEX')"  # NULL-only VALUES columns
            if c.kind in ("double", "real"):
                return f"${i}::double"
            return f"${i}::{t}"

        def val(v: Any, c: Col) -> Any:
            """Value `v` as the text the cast in `cast` reads back exactly."""
            if v is None:
                return None
            if c.kind == "binary":
                return bytes(v).hex()
            if c.kind in ("double", "real"):
                return repr(float(v))
            if c.kind in ("dec", "money"):
                return format(v, "f")
            if c.kind in ("ts", "tstz", "date", "time", "ts3"):
                return v.isoformat()
            if c.kind == "bool":
                return "true" if v else "false"
            return str(v)

        sel = ", ".join(cast(i + 1, c) for i, c in enumerate(cols))
        head = (
            f"insert into {self.ident_table(table)} "
            f"({', '.join(self.ident(n) for n in names)}) select {sel} from values "
        )
        per = max(1, 4000 // len(cols))
        for i in range(0, len(rows), per):
            chunk = rows[i : i + per]
            tup = "(" + ", ".join(["%s"] * len(cols)) + ")"
            sql = head + ", ".join([tup] * len(chunk))
            self.exec(
                sql, [val(v, c) for r in chunk for v, c in zip(r, cols, strict=True)]
            )

    def fetch_expr(self, name: str, c: Col) -> str:
        """Selected so the oracle sees the stored value exactly."""
        if c.kind == "tstz":
            return f"to_varchar(convert_timezone('UTC', {self.ident(name)}), 'YYYY-MM-DD\"T\"HH24:MI:SS.FF6')"
        if c.kind == "double" or c.kind == "real":
            return self.ident(name)
        return self.ident(name)

    def fetch(self, table, names, cols, where=None):
        """Every row back; TIMESTAMP_TZ read as UTC text."""
        out = []
        for r in super().fetch(table, names, cols, where):
            r = list(r)
            for i, c in enumerate(cols):
                if c.kind == "tstz" and r[i] is not None:
                    r[i] = dt.datetime.fromisoformat(r[i]).replace(tzinfo=fh.UTC)
            out.append(tuple(r))
        return out

    def drop(self, table: str) -> None:
        """Drop the table."""
        self.exec(f"drop table if exists {self.ident_table(table)}")

    def close(self) -> None:
        """Close the writer's connection."""
        self.d.close()


def open_engine(name: str, urls: dict[str, str]) -> fh.Engine:
    """A writer for `name`, Snowflake included."""
    if name == "snowflake":
        return Snowflake(urls["snowflake"])
    return fh.open_engine(name, urls)


def urls() -> dict[str, str]:
    """Engine URLs from the test suite's environment variables."""
    from conftest import MYSQL_URL, PG_URL

    out = {"postgres": PG_URL, "mysql": MYSQL_URL, "duckdb": ""}
    for name, var in (
        ("mssql", "PARITY_TEST_MSSQL"),
        ("bigquery", "PARITY_TEST_BIGQUERY"),
        ("snowflake", "PARITY_TEST_SNOWFLAKE"),
    ):
        if os.environ.get(var):
            out[name] = os.environ[var]
    return out


# ------------------------------------------------------------ generation


@dataclass
class CaseH(fh.Case):
    where_a: str | None = None
    where_b: str | None = None
    column_map: dict[str, str] = field(default_factory=dict)
    names_b: list[str] | None = None  # side B's stored names, when renamed
    verify_sample: int = 10_000
    where_desc: str = ""

    def describe(self) -> str:
        """The base description plus the round-H knobs."""
        return (
            super().describe() + f" vs={self.verify_sample} where_a={self.where_a!r} "
            f"where_b={self.where_b!r} map={self.column_map}"
        )


KEY_SHAPES = [
    "int",
    "int",
    "bigint",
    "text",
    "text",
    "dec",
    "date",
    "ts",
    "uuid",
    "int+text",
    "text+date",
    "bigint+int",
    "dec+text",
    "uuid+int",
    "int+text+date",
    "ts+int",
    "binary",
    "time+int",
]


def kinds_for(engines: set[str]) -> list[str]:
    """Every value kind both engines of the pair store."""
    out = [*fh.KINDS, "time", "binary", "uuid"]
    if engines <= JSON_OK:
        out.append("json")
    # Native variants of the same logical types (smallint, tinyint, ubigint,
    # char(n), latin1/varchar, money, millisecond timestamps), each where
    # every engine of the pair has one. Half the weight of the core kinds.
    variants = ["small", "tiny", "ubig", "char", "ascii", "money", "ts3"]
    for k in variants:
        if all(
            e in fh.EXTRA_DDL[k] if e != "snowflake" else k != "ubig" for e in engines
        ):
            out.append(k)
    banned = set().union(*(NO_KIND.get(e, set()) for e in engines))
    return [k for k in out if k not in banned]


def random_case_h(seed: int, a: str, b: str, max_rows: int = 120) -> CaseH:
    """One reproducible round-H case from `seed`."""
    rng = random.Random(seed)
    engines = {a, b}
    fs = rng.choice([0, 1, 2, 3, 6, 6, 6, 6, 9, 12, 15, 18])
    if "mssql" in engines:
        fs = min(fs, 18)
    shapes = [s for s in KEY_SHAPES if not ("binary" in s and "bigquery" in engines)]
    key_shape = rng.choice(shapes)
    key = [Col(f"k{i}", kind) for i, kind in enumerate(key_shape.split("+"))]
    # A DECIMAL key with no integer digits (p == s) is generated too: RT-HX1
    # (DuckDB rendered `.25`) is fixed.
    key = [
        replace(c, p=(p := rng.randint(2, 30)), s=rng.randint(0, min(p, 8)))
        if c.kind == "dec"
        else c
        for c in key
    ]
    kinds = kinds_for(engines)
    shape = rng.random()
    if shape < 0.03:
        width = rng.randint(65, 100)
    elif shape < 0.25:
        width = rng.randint(7, 40)
    else:
        width = rng.randint(1, 6)
    n = rng.choice([0, 1, 2, 5, 20, 60, max_rows, max_rows])
    if width > 40:
        n = min(n, 15)
    elif shape > 0.96:
        n = 1500
    cols: list[Col] = []
    for i in range(width):
        kind = rng.choice(kinds)
        name = f"c{i:03d}" if width > 9 else f"c{i}"
        if rng.random() < 0.05:
            odd = (
                ["Mixed_Case", "Select"]
                if "bigquery" in engines
                else ["Mixed Case", "naïve", "select", "a-b", "x.y", "Order"]
            )
            name = rng.choice(odd) + str(i)
        b_kind = None
        if kind == "dec":
            p = rng.randint(1, 38)
            s = rng.randint(0, min(p, 12))
            # Scale 0 included: Snowflake reads NUMBER(p,0) as INTEGER and
            # the other engines DECIMAL(p,0) as DECIMAL, compared by value
            # since RT-HX2 was fixed.
            p = min(p, s + 38 - fs)
            if rng.random() < 0.15:
                b_kind = "double"
                p = min(p, s + 34)
            cols.append(Col(name, kind, p, s, b_kind))
            continue
        if kind == "int" and rng.random() < 0.2:
            b_kind = "bigint"
        # A migration's type change that keeps every value: must compare equal.
        widen = {
            "small": "int",
            "tiny": "int",
            "char": "text",
            "ascii": "text",
            "money": "dec",
            "ts3": "ts",
        }
        if kind in widen and rng.random() < 0.3:
            b_kind = widen[kind]
            if kind == "money":
                cols.append(Col(name, kind, 19, 4, b_kind))
                continue
        if kind == "real" and rng.random() < 0.3:
            b_kind = "double"
        cols.append(Col(name, kind, b_kind=b_kind))
    keys = fh.gen_key(rng, key, n, engines)
    if any(c.kind == "text" for c in key) and rng.random() < 0.4:
        keys = hostile_text_keys(rng, key, keys)
    null_rate = rng.choice([0.0, 0.1, 0.3])
    rows = []
    for k in keys:
        vals = [
            None if rng.random() < null_rate else fh.gen_value(rng, c, engines, fs, set())
            for c in cols
        ]
        rows.append((*k, *vals))
    case = CaseH(
        seed=seed,
        a_engine=a,
        b_engine=b,
        key=key,
        cols=cols,
        rows_a=list(rows),
        rows_b=list(rows),
        float_scale=fs,
        bisection_factor=rng.choice([2, 2, 3, 4, 7, 16, 32, 64]),
        threshold=rng.choice([1, 1, 2, 5, 50, 10_000]),
        max_diffs=rng.choice([None, None, None, None, 0, 1, 3, 10]),
        b_upper=rng.random() < 0.2,
        verify_sample=rng.choice([10_000, 10_000, 0, 1, 3, 50]),
    )
    if cols and rng.random() < 0.12:
        case.columns_arg = rng.sample([c.name for c in cols], rng.randint(1, len(cols)))
    elif cols and rng.random() < 0.12:
        case.exclude_arg = [rng.choice(cols).name]
    fh.mutate(rng, case, engines, set())
    extra_mutations(rng, case, engines)
    if rng.random() < 0.25:
        plan_map(rng, case)
    if rng.random() < 0.3:
        plan_where(rng, case)
    return case


def hostile_text_keys(
    rng: random.Random, key: list[Col], keys: list[tuple]
) -> list[tuple]:
    """Text key parts that differ only by case, trailing or leading space,
    tab, accent, normalisation form, a backslash or a separator - the keys a
    case- or pad-insensitive collation would merge."""
    import unicodedata

    stems = ["a", "abc", "Straße", "é", "x y", "日本", "", "0", "İ", "ß"]

    def variant(s: str) -> str:
        """A random near-twin of `s` that a lax collation would merge with it."""
        return rng.choice(
            [
                s,
                s.upper(),
                s.swapcase(),
                s + " ",
                " " + s,
                s + "\t",
                s + "  ",
                unicodedata.normalize("NFD", s),
                s + "́",
                s + "\\",
                s + "\\s",
                s + "\x1f",
                s + "​",
                s.casefold(),
            ]
        )

    out, seen = [], set()
    for k in keys:
        k = list(k)
        for j, c in enumerate(key):
            if c.kind == "text":
                k[j] = variant(rng.choice(stems))
        t = tuple(k)
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


EXTRA_OPS = [
    "time_micro",
    "bytes_tweak",
    "uuid_tweak",
    "json_tweak",
    "colswap",
    "keytweak",
]


def extra_mutations(rng: random.Random, case: CaseH, engines: set[str]) -> None:
    """0-2 mutations of the new kinds, of a key, or two columns swapped."""
    nkey = len(case.key)
    target_b = rng.random() < 0.8
    rows = case.rows_b if target_b else case.rows_a
    tag = "B" if target_b else "A"
    for _ in range(rng.choice([0, 0, 1, 2])):
        if not rows:
            return
        op = rng.choice(EXTRA_OPS)
        i = rng.randrange(len(rows))
        r = list(rows[i])
        if op == "colswap":
            # Two columns of the same kind exchange their values in one row.
            by_kind = collections.defaultdict(list)
            for j, c in enumerate(case.cols):
                by_kind[(c.kind, c.p, c.s, c.b_kind)].append(nkey + j)
            groups = [g for g in by_kind.values() if len(g) >= 2]
            if not groups:
                continue
            j1, j2 = rng.sample(rng.choice(groups), 2)
            r[j1], r[j2] = r[j2], r[j1]
        elif op == "keytweak":
            # The smallest change to one key part: case, a trailing space, NFD,
            # a microsecond, the last decimal digit.
            j = rng.randrange(nkey)
            c = case.key[j]
            v = r[j]
            if c.kind == "text":
                import unicodedata

                nv = rng.choice(
                    [v.swapcase(), v + " ", unicodedata.normalize("NFD", v) + "́"]
                )
            elif c.kind == "ts":
                nv = v + dt.timedelta(microseconds=1)
            elif c.kind == "dec":
                nv = v + Decimal(1).scaleb(-c.s)
                if abs(nv) >= Decimal(10) ** (c.p - c.s):
                    continue
            elif c.kind in ("int", "bigint"):
                nv = v + 1
                if nv >= (2**31 if c.kind == "int" else 2**63):
                    continue
            elif c.kind == "uuid":
                nv = uuid.UUID(int=v.int ^ 1)
            else:
                continue
            present = {
                tuple(fh.key_text(x[m], case.key[m].kind) for m in range(nkey))
                for x in [*case.rows_a, *case.rows_b]
            }
            r[j] = nv
            if tuple(fh.key_text(r[m], case.key[m].kind) for m in range(nkey)) in present:
                continue
        else:
            cand = [
                nkey + j
                for j, c in enumerate(case.cols)
                if c.kind
                == {
                    "time_micro": "time",
                    "bytes_tweak": "binary",
                    "uuid_tweak": "uuid",
                    "json_tweak": "json",
                }[op]
            ]
            if not cand:
                continue
            ci = rng.choice(cand)
            nv = fh._mutate_cell(
                rng, op, case.cols[ci - nkey], r[ci], case.float_scale, engines
            )
            if nv is fh._SKIP:
                continue
            r[ci] = nv
        rows[i] = tuple(r)
        case.mutations.append(f"{tag}:{op}@{i}")


def plan_map(rng: random.Random, case: CaseH) -> None:
    """Rename some of side B's columns and pair them back with `--map`."""
    allc = case.all_cols
    names = [c.name.upper() if case.b_upper else c.name for c in allc]
    nkey = len(case.key)
    excluded = {e.casefold() for e in case.exclude_arg}
    candidates = [i for i in range(len(allc)) if allc[i].name.casefold() not in excluded]
    if case.columns_arg is not None:
        # A mapped column outside --columns is still fine; keys always count.
        pass
    if not candidates:
        return
    mode = rng.choice(["rename", "rename", "swap", "key"])
    if mode == "swap":
        by_kind = collections.defaultdict(list)
        for i in candidates[nkey:] if nkey < len(candidates) else []:
            c = allc[i]
            by_kind[(c.kind, c.p, c.s, c.b_kind)].append(i)
        groups = [g for g in by_kind.values() if len(g) >= 2]
        if groups:
            i, j = rng.sample(rng.choice(groups), 2)
            # B stores A's column i under A's name j and the other way round.
            names[i], names[j] = names[j], names[i]
            case.column_map = {allc[i].name: names[i], allc[j].name: names[j]}
            case.names_b = names
            return
        mode = "rename"
    pool = [i for i in candidates if (i < nkey) == (mode == "key")] or candidates
    chosen = rng.sample(pool, rng.randint(1, min(3, len(pool))))
    for i in chosen:
        new = rng.choice([allc[i].name + "_r", "R_" + allc[i].name, "renamed_" + str(i)])
        new = new.upper() if case.b_upper else new
        names[i] = new
        # The map is given in either case: matching folds case.
        case.column_map[allc[i].name] = rng.choice([new, new.swapcase()])
    case.names_b = names


def _pred(
    rng: random.Random, case: CaseH, side: str, eng: str, names: list[str]
) -> tuple[str, Any]:
    """One random predicate as (SQL for `eng`, a description)."""
    allc = case.all_cols
    ident = _ident(eng)
    j = rng.randrange(len(allc))
    c = allc[j].on_b() if side == "B" else allc[j]
    q = ident(names[j])
    r = rng.random()
    if c.kind in fh.INT_LIKE and r < 0.8:
        n = rng.choice([0, 1, -1, 5, 50, 100, 2**31 - 1])
        op = rng.choice([">", ">=", "<", "<=", "<>", "="])
        if rng.random() < 0.4:
            m = rng.choice([2, 3, 7])
            if eng == "bigquery":
                return f"mod({q}, {m}) = {rng.randrange(m)}", ("mod", j)
            return f"{q} % {m} = {rng.randrange(m)}", ("mod", j)
        return f"{q} {op} {n}", ("cmp", j)
    if c.kind in fh.TEXT_LIKE and r < 0.7:
        lit = rng.choice(["", "a", "m", "x", "é", "it''s", "50%", "a_b", "日"])
        if c.kind == "ascii":  # MySQL refuses a latin1-utf8mb4 comparison
            lit = lit if lit.isascii() else "n"
        op = rng.choice(["<>", ">", "<", "=", "like"])
        if op == "like":
            return f"{q} like '{lit}%'", ("like", j)
        return f"{q} {op} '{lit}'", ("cmpt", j)
    if c.kind == "date" and r < 0.7:
        d = rng.choice(["2010-01-01", "2000-06-15", "1999-12-31"])
        lit = f"'{d}'" if eng == "mssql" else f"date '{d}'"
        return f"{q} >= {lit}", ("date", j)
    if c.kind in ("dec", "double", "real", "money") and r < 0.7:
        return f"{q} > {rng.choice(['0', '0.5', '-1', '1e3'])}", ("num", j)
    if c.kind == "bool" and r < 0.5 and eng != "mssql":
        return f"{q}", ("bool", j)
    neg = rng.random() < 0.5
    return f"{q} is {'not ' if neg else ''}null", ("null", j)


def _ident(eng: str) -> Callable[[str], str]:
    """Identifier quoting in the engine's SQL."""
    if eng in ("mysql", "bigquery"):
        return lambda s: "`" + s + "`"
    if eng == "mssql":
        return lambda s: "[" + s + "]"
    return lambda s: '"' + s.replace('"', '""') + '"'


def _where_for(rng: random.Random, case: CaseH, side: str, names: list[str]) -> str:
    """A predicate tree of one to three leaves for one side."""
    eng = case.b_engine if side == "B" else case.a_engine
    leaves = [_pred(rng, case, side, eng, names)[0] for _ in range(rng.randint(1, 3))]
    out = leaves[0]
    for leaf in leaves[1:]:
        op = rng.choice(["and", "or", "and not"])
        out = f"({out}) {op} ({leaf})" if op != "and not" else f"({out}) and not ({leaf})"
    if rng.random() < 0.1:
        out = " \n " + out + "\t"
    return out


def plan_where(rng: random.Random, case: CaseH) -> None:
    """A row filter on one or both sides, through the same RNG."""
    names_a = [c.name for c in case.all_cols]
    names_b = case.names_b or [
        c.name.upper() if case.b_upper else c.name for c in case.all_cols
    ]
    which = rng.choice(["both_same", "both_same", "a", "b", "both"])
    state = rng.getstate()
    if which in ("both_same", "a", "both"):
        case.where_a = _where_for(rng, case, "A", names_a)
    if which == "both_same":
        rng.setstate(state)  # the same predicate, spelled for B's names/engine
    if which in ("both_same", "b", "both"):
        case.where_b = _where_for(rng, case, "B", names_b)


# ---------------------------------------------------------------- runner


@dataclass
class OutcomeH(fh.Outcome):
    sample_failures: list[str] = field(default_factory=list)
    sample_notes: list[str] = field(default_factory=list)
    sample_error: str | None = None
    unexplained: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:  # type: ignore[override]
        """The base verdict, and the sample check found nothing either.

        Stopping at exactly `max_diffs` differences (0 included) may flag the
        answer partial: conservative, never a false verdict, so accepted when
        what it lists is complete and it does not claim identical."""
        if self.sample_failures:
            return False
        limit = self.case.max_diffs
        if (
            self.error is None
            and self.got is not None
            and limit is not None
            and len(self.expected) == limit
            and self.truncated
            and not self.identical
            and self.got <= self.expected
        ):
            return True
        return fh.Outcome.ok.fget(self)  # type: ignore[attr-defined]

    def verdict(self) -> str:
        """As the base, plus SAMPLE FAIL when only the sample check objected."""
        v = fh.Outcome.verdict(self)
        if v == "OK" and self.sample_failures:
            return "SAMPLE FAIL"
        return v


def run_case_h(case: CaseH, ea: fh.Engine, eb: fh.Engine) -> OutcomeH:
    """Build both tables, run the oracle and `diff`, drop the tables."""
    import parity.engine as engine_mod

    tag = uuid.uuid4().hex[:6]
    table_a = f"rth_x_a_{case.seed}_{tag}"
    table_b = f"rth_x_b_{case.seed}_{tag}"
    cols = case.all_cols
    nkey = len(case.key)
    da = db = None
    try:
        names_a = ea.create(table_a, cols, nkey, upper=False)
        cols_b = [c.on_b() for c in cols]
        names_b = eb.create(table_b, cols_b, nkey, upper=case.b_upper, names=case.names_b)
        try:
            ea.load(table_a, names_a, cols, case.rows_a)
            eb.load(table_b, names_b, cols_b, [fh._to_b(r, cols) for r in case.rows_b])
        except Exception as exc:
            raise fh.LoadError(f"{case.describe()}: {exc}") from exc
        try:
            fa = ea.fetch(table_a, names_a, cols, case.where_a)
            fb = eb.fetch(table_b, names_b, cols_b, case.where_b)
        except Exception as exc:
            raise fh.LoadError(f"oracle fetch: {case.describe()}: {exc}") from exc
        for e in (ea, eb):
            if isinstance(e, fh.Duck):
                e.seal()
        expected = fh.oracle(case, fa, fb)
        da = get_dialect(ea.dialect_url(table_a), side="A", float_scale=case.float_scale)
        db = get_dialect(eb.dialect_url(table_b), side="B", float_scale=case.float_scale)
        seen: list[Any] = []
        original = engine_mod._verify

        def spy(v, *args, **kwargs):
            """Run the real sample check, then record its Verification."""
            original(v, *args, **kwargs)
            seen.append(v)

        engine_mod._verify = spy
        try:
            res = diff(
                da,
                db,
                ea.table_ref(table_a),
                eb.table_ref(table_b),
                key=[c.name for c in case.key],
                columns=case.columns_arg,
                exclude=case.exclude_arg,
                bisection_factor=case.bisection_factor,
                threshold=case.threshold,
                max_diffs=case.max_diffs,
                verify_sample=case.verify_sample,
                where_a=case.where_a,
                where_b=case.where_b,
                column_map=case.column_map or None,
            )
        except Exception as exc:
            return OutcomeH(
                case, expected, None, None, None, f"{type(exc).__name__}: {exc}"[:800]
            )
        finally:
            engine_mod._verify = original
        v = res.verification
        return OutcomeH(
            case,
            expected,
            fh.observed(res),
            res.identical,
            res.truncated,
            sample_failures=list(v.failures) if v else [],
            sample_notes=list(v.notes) if v else [],
            sample_error=v.error if v else None,
            unexplained=list(res.unexplained),
            warnings=list(res.warnings),
        )
    finally:
        for d in (da, db):
            if d is not None:
                try:
                    d.close()
                except Exception:  # noqa: S110
                    pass
        for e, t in ((ea, table_a), (eb, table_b)):
            try:
                e.drop(t)
            except Exception:  # noqa: S110
                pass


def shrink_h(
    case: CaseH, ea, eb, still_bad: Callable[[OutcomeH], bool], budget: int = 150
) -> CaseH:
    """Greedy: drop keys (halves, then singles), then columns, within `budget` runs."""
    nkey = len(case.key)
    runs = 0

    def bad(c: CaseH) -> bool:
        """Does `c` still show the failure? Counts against the budget."""
        nonlocal runs
        runs += 1
        try:
            return still_bad(run_case_h(c, ea, eb))
        except fh.LoadError:
            return False

    def keys_of(c: CaseH) -> list[tuple]:
        """The distinct keys of `c`, both sides, in first-seen order."""
        return list(dict.fromkeys(tuple(r[:nkey]) for r in [*c.rows_a, *c.rows_b]))

    best = case
    changed = True
    while changed and runs < budget:
        changed = False
        chunk = max(1, len(keys_of(best)) // 2)
        while chunk >= 1 and runs < budget:
            i = 0
            while i < len(keys_of(best)) and runs < budget:
                drop = set(keys_of(best)[i : i + chunk])
                trial = replace(
                    best,
                    rows_a=[r for r in best.rows_a if tuple(r[:nkey]) not in drop],
                    rows_b=[r for r in best.rows_b if tuple(r[:nkey]) not in drop],
                    mutations=list(best.mutations),
                )
                if bad(trial):
                    best, changed = trial, True
                else:
                    i += chunk
            chunk //= 2
        for ci in range(len(best.cols) - 1, -1, -1):
            if len(best.cols) <= 1 or runs >= budget:
                break
            name = best.cols[ci].name
            if (
                best.columns_arg and name in best.columns_arg
            ) or name in best.exclude_arg:
                continue
            if name in best.column_map or best.where_a or best.where_b:
                continue
            pos = nkey + ci
            trial = replace(
                best,
                cols=[c for j, c in enumerate(best.cols) if j != ci],
                rows_a=[r[:pos] + r[pos + 1 :] for r in best.rows_a],
                rows_b=[r[:pos] + r[pos + 1 :] for r in best.rows_b],
                mutations=list(best.mutations),
                names_b=(best.names_b[:pos] + best.names_b[pos + 1 :])
                if best.names_b
                else None,
            )
            if bad(trial):
                best, changed = trial, True
    return best


def all_pairs(names: list[str]) -> list[tuple[str, str]]:
    """Every pair including same-engine pairs, in a stable order."""
    return [(a, b) for i, a in enumerate(names) for b in names[i:]]


def report(out: OutcomeH) -> str:
    """A multi-line repro of one failing outcome."""
    c = out.case
    lines = [
        f"[{out.verdict()}] {c.describe()}",
        f"    error={out.error}",
        f"    missed={sorted(map(repr, out.missed))[:6]}",
        f"    extra={sorted(map(repr, out.extra))[:6]}",
        f"    sample_failures={out.sample_failures[:3]} notes={out.sample_notes[:3]}",
        f"    unexplained={out.unexplained[:3]} identical={out.identical} truncated={out.truncated}",
    ]
    if len(c.rows_a) + len(c.rows_b) <= 10:
        lines.append(f"    rows_a={c.rows_a!r}"[:2500])
        lines.append(f"    rows_b={c.rows_b!r}"[:2500])
    return "\n".join(lines)


def main() -> int:
    """Run the campaign until `--minutes` or `--cases` per pair."""
    # Repros carry any Unicode; a cp1252 console must not end the campaign.
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=100)
    ap.add_argument(
        "--minutes", type=float, default=0, help="per pair, overrides --cases"
    )
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument(
        "--pairs", default="", help="comma list like mssql-postgres,duckdb-duckdb"
    )
    ap.add_argument("--max-rows", type=int, default=120)
    ap.add_argument("--no-shrink", action="store_true")
    ap.add_argument("--log", default="")
    ap.add_argument("--start", type=int, default=0)
    args = ap.parse_args()
    u = urls()
    if args.pairs:
        todo = [tuple(p.split("-")) for p in args.pairs.split(",")]
    else:
        todo = all_pairs([n for n in LOCAL if n in u])
    logf = open(args.log, "a", encoding="utf-8") if args.log else None
    bad = 0
    for pi, (a, b) in enumerate(todo):
        ea, eb = open_engine(a, u), open_engine(b, u)
        t0 = time.time()
        tally: collections.Counter = collections.Counter()
        i = args.start
        while True:
            if args.minutes:
                if time.time() - t0 > args.minutes * 60:
                    break
            elif i >= args.start + args.cases:
                break
            seed = args.seed * 1_000_000 + pi * 100_000 + i
            i += 1
            sa, sb = (a, b) if seed % 2 or a == b else (b, a)
            x, y = (ea, eb) if (sa, sb) == (a, b) else (eb, ea)
            case = random_case_h(seed, sa, sb, args.max_rows)
            try:
                out = run_case_h(case, x, y)
            except fh.LoadError as exc:
                tally["LOADERR"] += 1
                msg = f"[LOADERR] seed={seed} {sa}->{sb}: {str(exc)[-700:]}"
                print(msg)
                if logf:
                    logf.write(msg + "\n")
                    logf.flush()
                continue
            except Exception:  # a harness crash must not end the campaign
                import traceback

                tally["CRASH"] += 1
                msg = f"[CRASH] seed={seed} {sa}->{sb}:\n{traceback.format_exc()[-1500:]}"
                print(msg)
                if logf:
                    logf.write(msg + "\n")
                    logf.flush()
                continue
            v = out.verdict()
            tally[v] += 1
            tally["with_diffs"] += bool(out.expected)
            tally["where"] += bool(case.where_a or case.where_b)
            tally["map"] += bool(case.column_map)
            if v != "OK":
                bad += 1
                if not args.no_shrink and v != "ERROR":
                    try:
                        small = shrink_h(case, x, y, lambda o, v=v: o.verdict() == v)
                        out = run_case_h(small, x, y)
                    except Exception as exc:  # keep the unshrunk repro
                        print(f"    (shrink failed: {type(exc).__name__}: {exc})"[:400])
                text = report(out)
                print(text)
                if logf:
                    logf.write(text + "\n")
                    logf.flush()
            sys.stdout.flush()
        line = f"{a}<->{b}: cases={i} {dict(tally)} in {time.time() - t0:.0f}s"
        print(line)
        if logf:
            logf.write(line + "\n")
            logf.flush()
        ea.close()
        eb.close()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
