"""Build an identical N-row dataset in DuckDB and PostgreSQL, then plant differences.

The two sides are identical *by construction*: the same `generate_series`
expression runs on both engines, so any difference the tool later reports is
either one we planted or a bug. That is the only way a benchmark of a parity
tool means anything.

    python demo/generate.py --rows 10000000
    python demo/generate.py --rows 10000000 --plant

Writes into demo/data/ (gitignored) and an `orders` table on each side.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

DEFAULT_PG = os.environ.get(
    "PARITY_TEST_PG", "postgres://parity:parity@127.0.0.1:5432/parity"
)

TABLE = "orders"

#: One expression, run verbatim on both engines. `generate_series` and every
#: function here is spelled identically in DuckDB and PostgreSQL, which is what
#: makes the two sides identical by construction rather than by copying.
SELECT = """
select i::bigint                                                    as id,
       (i % 9973)::integer                                          as customer_id,
       ((i * 7 % 1000000) / 100.0)::decimal(12,2)                   as amount,
       case when i % 3 = 0 then 'paid'
            when i % 3 = 1 then 'open'
            else 'void' end                                         as status,
       (i % 11 = 0)                                                 as is_refunded,
       (timestamp '2024-01-01 00:00:00'
            + (i % 31536000) * interval '1 second')                 as created_at,
       case when i % 13 = 0 then null
            else 'note ' || i::varchar end                          as note
from generate_series(1, {n}) as s(i)
"""

#: The four planted differences, applied to PostgreSQL (side A) only. Each is a
#: distinct failure mode a real migration produces.
PLANTS = [
    # A value that changed. The classic silent corruption.
    ("changed value", "update {t} set amount = amount + 0.01 where id = {changed}"),
    # A row that never arrived. Reported as only_in_b.
    ("deleted row", "delete from {t} where id = {deleted}"),
    # A row that should not exist. Reported as only_in_a, and its key is far
    # outside the range so it also exercises a sparse key space.
    (
        "extra row",
        (
            "insert into {t} values ({extra}, 1, 1.00, 'paid', false, "
            "timestamp '2024-01-01 00:00:00', 'extra')"
        ),
    ),
    # NULL became an empty string. The trap naive implementations pass over,
    # and a real migration bug class.
    ("NULL -> empty string", "update {t} set note = '' where id = {null_trap}"),
    # FALSE became NULL. Invisible to any tool whose boolean encoding sends
    # NULL down a CASE `else` branch.
    ("FALSE -> NULL", "update {t} set is_refunded = null where id = {bool_trap}"),
]


def plant_keys(n: int) -> dict[str, int]:
    """Keys to plant at. Spread across the range so the walk has to work."""
    return {
        "changed": max(1, int(n * 0.601)),
        "deleted": max(1, int(n * 0.0004)),
        "extra": 999_999_999,
        "null_trap": 13,
        "bool_trap": max(1, int(n * 0.87)),
    }


def build_duckdb(path: str, n: int) -> float:
    """Create the DuckDB side from scratch. Returns seconds taken."""
    import duckdb

    if os.path.exists(path):
        os.remove(path)
    started = time.perf_counter()
    con = duckdb.connect(path)
    try:
        con.execute(f"create table {TABLE} as {SELECT.format(n=n)}")
    finally:
        con.close()
    return time.perf_counter() - started


def build_postgres(url: str, n: int, index: bool) -> float:
    """Create the PostgreSQL side from the same expression. Returns seconds."""
    import psycopg

    started = time.perf_counter()
    con = psycopg.connect(url, autocommit=True)
    try:
        con.execute(f"drop table if exists {TABLE}")
        con.execute(f"create table {TABLE} as {SELECT.format(n=n)}")
        if index:
            # CLAUDE.md section 4.6: measured to make no difference, because the
            # cost is CPU-bound on MD5 over every row, not IO-bound on lookup.
            # Off by default so the benchmark reports the honest common case.
            con.execute(f"create unique index on {TABLE} (id)")
        con.execute(f"analyze {TABLE}")
    finally:
        con.close()
    return time.perf_counter() - started


def plant(url: str, n: int) -> list[str]:
    """Apply the five planted differences to side A. Returns their labels.

    Only side A is touched, so any difference the tool later reports is either
    one of these or a bug.
    """
    import psycopg

    keys = plant_keys(n)
    applied = []
    con = psycopg.connect(url, autocommit=True)
    try:
        for label, sql in PLANTS:
            con.execute(sql.format(t=TABLE, **keys))
            applied.append(label)
    finally:
        con.close()
    return applied


def main(argv: list[str] | None = None) -> int:
    """Build the dataset, or with --plant, seed it with known differences."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=10_000_000)
    parser.add_argument("--pg", default=DEFAULT_PG)
    parser.add_argument(
        "--duckdb", default=None, help="path to the DuckDB file (default demo/data/)"
    )
    parser.add_argument(
        "--plant", action="store_true", help="apply the planted differences to side A"
    )
    parser.add_argument(
        "--index",
        action="store_true",
        help="add a unique index on the key column (measured to be irrelevant)",
    )
    parser.add_argument(
        "--only", choices=["duckdb", "postgres"], help="build just one side"
    )
    args = parser.parse_args(argv)

    os.makedirs(DATA_DIR, exist_ok=True)
    duck_path = args.duckdb or os.path.join(DATA_DIR, "new.duckdb")

    if args.plant:
        applied = plant(args.pg, args.rows)
        keys = plant_keys(args.rows)
        print(f"planted {len(applied)} differences on side A (postgres):")
        names = ["changed", "deleted", "extra", "null_trap", "bool_trap"]
        # strict: if PLANTS and these key names ever drift apart, say so
        # rather than silently printing a short list.
        for label, key in zip(applied, names, strict=True):
            print(f"  {label:<22} id {keys[key]:,}")
        return 0

    print(f"building {args.rows:,} rows per side")
    if args.only != "postgres":
        secs = build_duckdb(duck_path, args.rows)
        size = os.path.getsize(duck_path) / 1e6
        print(f"  duckdb      {secs:6.1f}s  {size:,.0f} MB  {duck_path}")
    if args.only != "duckdb":
        secs = build_postgres(args.pg, args.rows, args.index)
        print(f"  postgresql  {secs:6.1f}s  {args.pg.rsplit('@', 1)[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
