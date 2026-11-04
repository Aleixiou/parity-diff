"""Run DuckDB-only checks under whatever duckdb library this interpreter has.

`test_versions.py` starts this script with the python of another virtualenv
(`PARITY_RTG_DUCKDB_PYTHONS`), so each DuckDB release is tested by the parity
code of this checkout. It prints one JSON object on stdout:

    python rtg_duck_probe.py <mode> <scratch dir>

- `cov`: the conformance harness's cells at float scale 6, by `native|key`.
- `scenarios`: the planted-difference battery, and the version-sensitive
  cases (list text, nanosecond timestamps before the epoch), as found keys.

The parity package and `tests/` must be on PYTHONPATH.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "conformance"))
sys.path.insert(0, str(HERE.parent))

from parity.dialects.base import get_dialect  # noqa: E402
from parity.engine import diff  # noqa: E402


def write(path: str, *statements: str) -> str:
    """A fresh DuckDB file holding `statements`."""
    if os.path.exists(path):
        os.remove(path)
    con = duckdb.connect(path)
    try:
        for sql in statements:
            con.execute(sql)
    finally:
        con.close()
    return path


def compare(pa: str, pb: str, key: str = "id") -> dict:
    """Diff main.t of two files; the found differences and the sample verdict."""
    a = get_dialect(f"duckdb:///{pa}", side="A")
    b = get_dialect(f"duckdb:///{pb}", side="B")
    try:
        r = diff(a, b, "main.t", "main.t", key=key)
    finally:
        a.close()
        b.close()
    v = r.verification
    return {
        "found": sorted([str(d.key), d.kind, sorted(d.columns)] for d in r.diffs),
        "identical": r.identical,
        "sample_failures": list(v.failures) if v else None,
        "sample_error": v.error if v else None,
    }


def scenarios(tmp: str) -> dict:
    """The planted battery and the version-sensitive cases."""
    out: dict = {"version": duckdb.__version__}
    rows = "select i, i * 1.25, 'n' || i, i % 2 = 0 from range(1, 301) r(i)"
    ddl = "create table t (id integer, amount decimal(12,2), name varchar, flag boolean)"
    pa = write(os.path.join(tmp, "pa.duckdb"), ddl, f"insert into t {rows}",
               "update t set name = null where id = 100")  # fmt: skip
    pb = write(
        os.path.join(tmp, "pb.duckdb"),
        ddl,
        f"insert into t {rows}",
        "update t set amount = 9.99 where id = 7",
        "delete from t where id = 50",
        "insert into t values (999999999, 1, 'new', true)",
        "update t set name = '' where id = 100",
        "update t set flag = null where id = 200",
    )
    out["battery"] = compare(pa, pb)
    lists = "create table t (id integer, l varchar[])"
    out["lists"] = compare(
        write(
            os.path.join(tmp, "la.duckdb"),
            lists,
            "insert into t values (1, ['NULL']), (2, ['a, b']), (3, ['x'])",
        ),
        write(
            os.path.join(tmp, "lb.duckdb"),
            lists,
            "insert into t values (1, [NULL]), (2, ['a', 'b']), (3, ['x'])",
        ),
    )
    dec = "create table t (id integer, x decimal(12,7))"
    out["decimal_half"] = compare(
        write(
            os.path.join(tmp, "da.duckdb"),
            dec,
            "insert into t values (1, 2.0000005), (2, -1.2345665), (3, 7)",
        ),
        write(
            os.path.join(tmp, "db.duckdb"),
            dec,
            "insert into t values (1, 2.0000000), (2, -1.2345660), (3, 7)",
        ),
    )
    ns = "create table t (id integer, ts timestamp_ns)"
    out["ns_before_epoch"] = compare(
        write(
            os.path.join(tmp, "na.duckdb"),
            ns,
            "insert into t values (1, '1969-12-31 23:59:59.9999999'), (2, '2024-01-01')",
        ),
        write(
            os.path.join(tmp, "nb.duckdb"),
            ns,
            "insert into t values (1, '1970-01-01 00:00:00'), (2, '2024-01-01')",
        ),
    )
    return out


def cov(tmp: str) -> dict:
    """The conformance harness's cells at scale 6, by `native|key`."""
    from cov_engines import DuckDBCov
    from cov_harness import run

    eng = DuckDBCov(tmp)
    try:
        runs = run(eng, (6,))
    finally:
        eng.drop_all()
    return {
        "version": duckdb.__version__,
        "cells": {f"{c.native}|{c.key}": [c.status, c.text] for c in runs[6].cells},
    }


def refuse(tmp: str) -> dict:
    """Open a DuckDB dialect; the error text if parity refuses this version."""
    import duckdb

    from parity.dialects.base import get_dialect

    try:
        get_dialect("duckdb:///:memory:", side="A").close()
    except ValueError as exc:
        return {"version": duckdb.__version__, "error": str(exc)}
    return {"version": duckdb.__version__, "error": None}


def main() -> None:
    """Dispatch on the mode and print the JSON result."""
    mode, tmp = sys.argv[1], sys.argv[2]
    result = {"cov": cov, "scenarios": scenarios, "refuse": refuse}[mode](tmp)
    sys.stdout.write("RTG-JSON " + json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
