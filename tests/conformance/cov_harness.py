"""Load hard values into one engine, render them, and check them two ways.

For every (native type, value) the harness records:

- `text`: what the dialect's own `normalize` renders in the engine, fetched
  through its own `fetch_text` / `fetched` (the bytes the checksum hashes);
- `ref`: `canonical_text(reference_token(...))` of the raw value read back
  through the dialect's `sample_select` / `sample_value` - the rules applied in
  Python to what the driver hands back, without a line of dialect SQL;
- `spec`: the same rules applied to the *input* value, before any engine or
  driver touched it.

Status of each cell: `agree` (text == ref), `disagree`, `refused` (the
engine raised, as it must for values the canonical form cannot render),
`unchecked` (the rules or the sample path cannot read the value), `unruled`
(an UNKNOWN-mapped type: no rule exists; only the text is recorded),
`unstorable` (the engine would not take the value - a property of the type,
not of parity) and `error` (an unexpected failure).

Results are written as JSON per (engine, scale) to `RESULTS`, where the
cross-engine test and `report.py` read them.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cov_engines import EXTRA, CovEngine, Native
from cov_known import REFUSED
from cov_values import GROUPS, REFUSE, Val

from parity.dialects.base import Dialect
from parity.reference import UNCHECKED, canonical_text, reference_token
from parity.types import Column, LogicalType

#: Where per-engine results go; `report.py` reads the same place.
RESULTS = Path(
    os.environ.get("PARITY_COV_RESULTS", str(Path(__file__).resolve().parent / "results"))
)


@dataclass
class Cell:
    """One value in one native column on one engine, at one scale."""

    native: str
    group: str
    key: str
    logical: str
    input: str
    status: str
    text: str | None = None
    ref: str | None = None
    spec: str | None = None
    detail: str = ""


@dataclass
class EngineRun:
    """Everything one engine produced at one scale."""

    engine: str
    scale: int
    cells: list[Cell] = field(default_factory=list)
    when: str = ""
    seconds: float = 0.0

    def save(self) -> Path:
        """Write this run as JSON under `RESULTS`."""
        RESULTS.mkdir(parents=True, exist_ok=True)
        path = RESULTS / f"{self.engine}-s{self.scale}.json"
        body = {
            "engine": self.engine,
            "scale": self.scale,
            "when": self.when,
            "seconds": round(self.seconds, 1),
            "cells": [asdict(c) for c in self.cells],
        }
        path.write_text(json.dumps(body, indent=1, ensure_ascii=True), encoding="utf-8")
        return path


def values_for(native: Native) -> list[Val]:
    """The values of a native's group that it can hold exactly."""
    vals = GROUPS.get(native.group) or EXTRA[native.group]
    return [v for v in vals if native.accepts(v)]


def _show(x: Any) -> str:
    """A short, printable repr of an input value."""
    r = repr(x)
    return r if len(r) <= 80 else r[:60] + f"...<{len(r)} chars>"


def _short(exc: BaseException) -> str:
    """The first line of an exception, bounded."""
    text = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {(text[0] if text else '')[:300]}"


class Session:
    """A dialect that can be reopened after an error aborted its transaction."""

    def __init__(self, engine: CovEngine, scale: int) -> None:
        """Open the engine's dialect at `scale`."""
        self.engine = engine
        self.scale = scale
        self.d: Dialect = engine.open(scale)

    def reset(self) -> None:
        """Roll back, or reopen if that is not enough (PostgreSQL aborts)."""
        with contextlib.suppress(Exception):  # the session is being replaced
            self.d.close()
        self.d = self.engine.open(self.scale)

    def close(self) -> None:
        """Close the dialect."""
        self.d.close()


def _select(
    s: Session, table: str, exprs: Sequence[str], ids: Sequence[int] | None
) -> dict[int, tuple[Any, ...]]:
    """`select id, exprs... from table [where id in ids]`, keyed by id."""
    d = s.d
    where = ""
    if ids is not None:
        where = " where " + d.quote("id") + " in (" + ", ".join(str(i) for i in ids) + ")"
    sql = f"select {d.quote('id')}, {', '.join(exprs)} from {d.qualify(table)}{where}"
    return {int(r[0]): tuple(r[1:]) for r in d.query(sql)}


def _robust(
    s: Session,
    table: str,
    exprs: Sequence[str],
    ids: Sequence[Sequence[int]],
) -> tuple[dict[int, tuple[Any, ...]], dict[tuple[int, int], str]]:
    """Select every expression for every id; isolate failures per column, then
    per row, so one refused value does not hide the others.

    `ids[j]` are the rows holding a value of column `j`. Returns (rows,
    {(id, column index): error}).
    """
    errors: dict[tuple[int, int], str] = {}
    try:
        return _select(s, table, exprs, None), errors
    except Exception:
        s.reset()
    rows: dict[int, list[Any]] = {i: [None] * len(exprs) for col in ids for i in col}
    for j, e in enumerate(exprs):
        try:
            got = _select(s, table, [e], None)
            for i, r in got.items():
                rows.setdefault(i, [None] * len(exprs))[j] = r[0]
            continue
        except Exception:
            s.reset()
        for i in ids[j]:
            try:
                got = _select(s, table, [e], [i])
                if i in got:
                    rows[i][j] = got[i][0]
            except Exception as exc:
                errors[(i, j)] = _short(exc)
                s.reset()
    return {i: tuple(r) for i, r in rows.items()}, errors


@dataclass
class Loaded:
    """One physical table: its natives, the value of each row per native."""

    table: str
    natives: list[Native]
    #: values[native index] = list of (row id, Val)
    values: list[list[tuple[int, Val]]]
    unstorable: dict[int, str]


def load(engine: CovEngine) -> list[Loaded]:
    """Create and fill the tables for every native of `engine`."""
    natives = engine.natives()
    if engine.per_native:
        groups = [[n] for n in natives]
    else:
        # Billed engines: one wide table, and the values that must be refused
        # apart, so the wide table still renders in a single query.
        groups = [[n for n in natives if n.group not in REFUSE]]
        groups += [[n] for n in natives if n.group in REFUSE]
    out: list[Loaded] = []
    for part in groups:
        table = engine.table_name("wide" if len(part) > 1 else part[0].slug)
        per = [values_for(n) for n in part]
        n_rows = max(len(v) for v in per)
        rows = []
        values: list[list[tuple[int, Val]]] = [[] for _ in part]
        for i in range(n_rows):
            row: list[Any] = [i + 1]
            for j, (n, vs) in enumerate(zip(part, per, strict=True)):
                if i < len(vs):
                    row.append(engine.bind(n, vs[i]))
                    values[j].append((i + 1, vs[i]))
                else:
                    row.append(None)
            rows.append(row)
        columns = [("id", "bigint" if engine.name != "bigquery" else "INT64")]
        columns += [(f"v_{j}", n.ddl) for j, n in enumerate(part)]
        bad = engine.build(table, columns, rows)
        out.append(Loaded(table, part, values, bad))
    return out


def _column(cols: dict[str, Column], j: int) -> Column:
    """The introspected Column for value column `j`."""
    for name, c in cols.items():
        if name.lower() == f"v_{j}":
            return c
    raise KeyError(f"v_{j}")


def _expected(d: Dialect, col: Column, value: Any, scale: int) -> tuple[str | None, str]:
    """canonical_text(reference_token(value)) for this column, or (None, why)."""
    try:
        token = reference_token(
            col.logical_type,
            value,
            scale,
            padded=d.padded_char(col),
            single_precision=d.single_precision(col),
        )
    except Exception as exc:
        return None, f"reference raised {_short(exc)}"
    if token is UNCHECKED:
        return None, f"reference cannot read {_show(value)}"
    return canonical_text(token), ""


def run(engine: CovEngine, scales: Sequence[int]) -> dict[int, EngineRun]:
    """Load every native once, then render and check at each scale."""
    started = time.monotonic()
    tables = load(engine)
    runs: dict[int, EngineRun] = {}
    raw_cache: dict[
        str, tuple[dict[int, tuple[Any, ...]], dict[tuple[int, int], str]]
    ] = {}
    for scale in scales:
        s = Session(engine, scale)
        er = EngineRun(
            engine.name, scale, when=dt.datetime.now(dt.timezone.utc).isoformat()
        )
        try:
            for t in tables:
                ref = engine.ref(t.table)
                cols = {c.name: c for c in s.d.columns(ref)}
                columns = [_column(cols, j) for j in range(len(t.natives))]
                ids = [[i for i, _ in vs] for vs in t.values]
                texts, terr = _robust(
                    s, ref, [s.d.fetch_text(s.d.normalize(c)) for c in columns], ids
                )
                if t.table not in raw_cache:
                    raw_cache[t.table] = _robust(
                        s, ref, [s.d.sample_select(c) for c in columns], ids
                    )
                raws, rerr = raw_cache[t.table]
                for j, (n, col) in enumerate(zip(t.natives, columns, strict=True)):
                    for i, v in t.values[j]:
                        er.cells.append(
                            _cell(
                                engine.name,
                                s.d,
                                n,
                                col,
                                v,
                                i,
                                j,
                                t,
                                texts,
                                terr,
                                raws,
                                rerr,
                                scale,
                            )
                        )
        finally:
            s.close()
        er.seconds = time.monotonic() - started
        runs[scale] = er
    return runs


def _cell(
    engine: str,
    d: Dialect,
    n: Native,
    col: Column,
    v: Val,
    i: int,
    j: int,
    t: Loaded,
    texts: dict[int, tuple[Any, ...]],
    terr: dict[tuple[int, int], str],
    raws: dict[int, tuple[Any, ...]],
    rerr: dict[tuple[int, int], str],
    scale: int,
) -> Cell:
    """Evaluate one (native, value)."""
    cell = Cell(n.ddl, n.group, v.key, col.logical_type.value, _show(v.value), "")
    if i in t.unstorable:
        cell.status, cell.detail = "unstorable", t.unstorable[i]
        return cell
    spec, _ = _expected(d, col, v.value, scale)
    cell.spec = spec
    if (i, j) in terr:
        documented = (engine, n.ddl, v.key) in REFUSED
        cell.status = "refused" if n.group in REFUSE or documented else "error"
        cell.detail = terr[(i, j)]
        return cell
    raw_text = texts.get(i, (None,) * (j + 1))[j]
    cell.text = d.fetched(raw_text) if raw_text is not None else None
    if cell.text is None:
        cell.status, cell.detail = "error", "the canonical text was SQL NULL"
        return cell
    if n.group in REFUSE:
        cell.status = "disagree"
        cell.detail = "rendered a value the canonical form must refuse"
        return cell
    if col.logical_type is LogicalType.UNKNOWN:
        cell.status = "unruled"
        return cell
    if (i, j) in rerr:
        cell.status, cell.detail = "unchecked", "sample select failed: " + rerr[(i, j)]
        return cell
    raw = d.sample_value(col, raws[i][j])
    expected, why = _expected(d, col, raw, scale)
    cell.ref = expected
    if expected is None:
        cell.status, cell.detail = "unchecked", why + f" (sample read {_show(raw)})"
        return cell
    cell.status = "agree" if cell.text == expected else "disagree"
    if cell.status == "disagree":
        cell.detail = f"sample read {_show(raw)}"
    return cell
