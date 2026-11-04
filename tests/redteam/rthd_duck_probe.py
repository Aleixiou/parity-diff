"""Round H DuckDB checks run under another duckdb library (see test_round_h_duckdb.py).

    python rthd_duck_probe.py <scratch dir>

Prints `RTHD-JSON {...}`: for each case, the differences the diff found.
The parity package must be on PYTHONPATH.
"""

from __future__ import annotations

import json
import os
import sys

import duckdb

from parity.dialects.base import get_dialect
from parity.engine import diff

#: (case id, column type, value on side A, value on side B) - row 1 differs.
CASES = [
    ("int-list-empty-vs-null", "INTEGER[]", "[]", "NULL"),
    ("varchar-list-empty-vs-null", "VARCHAR[]", "[]", "NULL"),
    ("date-list-empty-vs-null", "DATE[]", "[]", "NULL"),
    ("varchar-list-empty-vs-empty-string", "VARCHAR[]", "[]", "['']"),
    ("varchar-list-null-vs-NULL", "VARCHAR[]", "[NULL]", "['NULL']"),
    ("varchar-list-comma", "VARCHAR[]", "['a, b']", "['a', 'b']"),
    ("timetz-offset-seconds", "TIMETZ", "'12:00:00+05:00:30'", "'12:00:00+05:30'"),
    (
        "union-tag",
        "UNION(n INTEGER, s VARCHAR)",
        "union_value(n := 1)",
        "union_value(s := '1')",
    ),
    (
        "ns-timestamp",
        "TIMESTAMP_NS",
        "'2020-01-01 00:00:00.000000001'",
        "'2020-01-01 00:00:00.000000002'",
    ),
    ("interval-month-vs-days", "INTERVAL", "interval '1 month'", "interval '30 days'"),
    ("decimal-round", "DECIMAL(18,7)", "2.0000005", "2.0000004"),
]


def write(path: str, *statements: str) -> str:
    """A fresh DuckDB file."""
    if os.path.exists(path):
        os.remove(path)
    con = duckdb.connect(path)
    try:
        for sql in statements:
            con.execute(sql)
    finally:
        con.close()
    return path


def main(tmp: str) -> dict:
    """Every case, compared; the found keys or the error."""
    out: dict = {"version": duckdb.__version__, "cases": {}}
    for name, typ, a, b in CASES:
        ddl = f"create table t (id int, v {typ})"
        pa = write(
            os.path.join(tmp, f"{name}_a.duckdb"),
            ddl,
            f"insert into t values (1, {a}), (2, {a})",
        )
        pb = write(
            os.path.join(tmp, f"{name}_b.duckdb"),
            ddl,
            f"insert into t values (1, {b}), (2, {a})",
        )
        try:
            da = get_dialect(f"duckdb:///{pa}", side="A")
            db = get_dialect(f"duckdb:///{pb}", side="B")
        except ValueError as exc:
            out["cases"][name] = {"refused": str(exc)}
            continue
        try:
            r = diff(da, db, "main.t", "main.t", key="id", verify_sample=0)
            out["cases"][name] = {"found": sorted([d.key, d.kind] for d in r.diffs)}
        except Exception as exc:
            out["cases"][name] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        finally:
            da.close()
            db.close()
    return out


if __name__ == "__main__":
    print("RTHD-JSON " + json.dumps(main(sys.argv[1])))
